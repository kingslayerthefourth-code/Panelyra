"""Shared durable runtime records for the local automation suite."""

from __future__ import annotations

import json
import os
import fcntl
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit


RUNS_DIR = Path(__file__).resolve().parent / "runs"
BROWSER_LOCK_PATH = Path(tempfile.gettempdir()) / "panelyra-browser.lock"
ACTIVE_RUN_STATUSES = {"starting", "running"}


class BrowserBusyError(RuntimeError):
    """Raised when another Panelyra workflow owns the shared Chrome profile."""


def acquire_browser_lock(
    bot_type: str,
    lock_path: Path = BROWSER_LOCK_PATH,
) -> TextIO:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        owner_text = handle.read().strip()
        handle.close()
        owner = "another Panelyra workflow"
        if owner_text:
            try:
                payload = json.loads(owner_text)
                owner = str(payload.get("bot_type") or owner)
            except json.JSONDecodeError:
                pass
        raise BrowserBusyError(
            f"Chrome is already in use by {owner}. Stop that run or wait for it to finish, then retry."
        ) from exc

    handle.seek(0)
    handle.truncate()
    json.dump(
        {
            "bot_type": bot_type,
            "pid": os.getpid(),
            "started_at": int(time.time()),
        },
        handle,
    )
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def release_browser_lock(handle: TextIO | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    try:
        handle.close()
    except OSError:
        pass


def browser_lock_owner(lock_path: Path = BROWSER_LOCK_PATH) -> dict[str, Any]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        try:
            payload = json.loads(handle.read() or "{}")
        except json.JSONDecodeError:
            payload = {}
        handle.close()
        return payload if isinstance(payload, dict) else {}
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        return {}


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class RunRecord:
    run_id: str
    bot_type: str
    status: str
    started_at: int
    updated_at: int
    settings: dict[str, Any] = field(default_factory=dict)
    stage: str = "created"
    progress_current: int = 0
    progress_total: int = 0
    message: str = ""
    stop_reason: str = ""
    log_path: str = ""
    pid: int = 0

    @classmethod
    def create(cls, bot_type: str, settings: dict[str, Any], log_path: Path) -> "RunRecord":
        now = int(time.time())
        return cls(
            run_id=f"{bot_type}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
            bot_type=bot_type,
            status="starting",
            started_at=now,
            updated_at=now,
            settings=settings,
            log_path=str(log_path),
        )


class RunStore:
    def __init__(self, record: RunRecord, root: Path = RUNS_DIR) -> None:
        self.record = record
        self.root = root
        self.path = root / f"{record.run_id}.json"
        self.checkpoint_path = root / f"{record.run_id}.checkpoint.json"
        self.save()

    @classmethod
    def load(cls, path: Path) -> "RunStore":
        payload = json.loads(path.read_text(encoding="utf-8"))
        store = cls.__new__(cls)
        store.record = RunRecord(**payload)
        store.root = path.parent
        store.path = path
        store.checkpoint_path = path.with_name(path.stem + ".checkpoint.json")
        return store

    def save(self) -> None:
        self.record.updated_at = int(time.time())
        atomic_write_json(self.path, asdict(self.record))

    def update(self, **changes: Any) -> None:
        for key, value in changes.items():
            if hasattr(self.record, key):
                setattr(self.record, key, value)
        self.save()

    def checkpoint(self, stage: str, **payload: Any) -> None:
        self.update(stage=stage, message=str(payload.get("message", self.record.message)))
        atomic_write_json(
            self.checkpoint_path,
            {
                "run_id": self.record.run_id,
                "bot_type": self.record.bot_type,
                "stage": stage,
                "updated_at": int(time.time()),
                **payload,
            },
        )


def list_run_records(root: Path = RUNS_DIR, limit: int = 100) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name.endswith(".checkpoint.json"):
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(records) >= limit:
            break
    return records


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def update_run_record_by_pid(
    pid: int,
    *,
    status: str,
    stage: str,
    stop_reason: str,
    root: Path = RUNS_DIR,
) -> bool:
    if pid <= 0 or not root.exists():
        return False
    for path in root.glob("*.json"):
        if path.name.endswith(".checkpoint.json"):
            continue
        try:
            store = RunStore.load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if store.record.pid != pid:
            continue
        store.update(status=status, stage=stage, stop_reason=stop_reason)
        return True
    return False


def reconcile_stale_run_records(root: Path = RUNS_DIR) -> int:
    if not root.exists():
        return 0
    reconciled = 0
    now = int(time.time())
    for path in root.glob("*.json"):
        if path.name.endswith(".checkpoint.json"):
            continue
        try:
            store = RunStore.load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        record = store.record
        if record.status not in ACTIVE_RUN_STATUSES:
            continue
        startup_expired = record.pid <= 0 and now - record.started_at > 30
        process_exited = record.pid > 0 and not process_exists(record.pid)
        if not startup_expired and not process_exited:
            continue
        store.update(
            status="failed",
            stage="process-exited",
            stop_reason="The automation process exited before it could finalize its run.",
        )
        reconciled += 1
    return reconciled


def normalize_web_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("//"):
        return f"https:{normalized}"
    parsed = urlsplit(normalized)
    if normalized and "://" not in normalized:
        return f"https://{normalized}"
    if parsed.scheme.lower() in {"http", "https"}:
        return f"{parsed.scheme.lower()}:{normalized.split(':', 1)[1]}"
    return normalized


def preflight_url(value: str) -> list[str]:
    normalized = normalize_web_url(value)
    if not normalized:
        return ["URL is required."]
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ["Enter a valid web address."]
    return []


def reporter_from_environment() -> RunStore | None:
    path_value = (
        os.getenv("PANELYRA_RUN_RECORD", "").strip()
        or os.getenv("ASURA_RUN_RECORD", "").strip()
    )
    if not path_value:
        return None
    path = Path(path_value)
    try:
        return RunStore.load(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
