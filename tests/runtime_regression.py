#!/usr/bin/env python3
"""Regressions for browser ownership and durable run lifecycle state."""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from automation_runtime import (
    BrowserBusyError,
    RunRecord,
    RunStore,
    TrackedProcessRef,
    acquire_browser_lock,
    active_run_records,
    macos_sleep_guard_command,
    reconcile_stale_run_records,
    release_browser_lock,
)


def hold_browser_lock(lock_path: Path, ready: multiprocessing.Event, release: multiprocessing.Event) -> None:
    handle = acquire_browser_lock("Capture regression holder", lock_path)
    ready.set()
    release.wait(timeout=10)
    release_browser_lock(handle)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        lock_path = root / "browser.lock"
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=hold_browser_lock,
            args=(lock_path, ready, release),
        )
        holder.start()
        assert ready.wait(timeout=5)
        try:
            acquire_browser_lock("Competing workflow", lock_path)
        except BrowserBusyError as exc:
            assert "Capture regression holder" in str(exc)
        else:
            raise AssertionError("A second workflow acquired the shared browser lock.")
        finally:
            release.set()
            holder.join(timeout=5)
        assert holder.exitcode == 0

        handle = acquire_browser_lock("Workflow after release", lock_path)
        release_browser_lock(handle)

        runs_root = root / "runs"
        record = RunRecord.create("download", {"start_url": "https://reader.test"}, root / "run.log")
        store = RunStore(record, root=runs_root)
        store.update(status="running", stage="opening-browser", pid=99_999_999)
        assert reconcile_stale_run_records(runs_root) == 1
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["status"] == "failed"
        assert payload["stage"] == "process-exited"

        live_record = RunRecord.create("comment", {"start_url": "https://reader.test"}, root / "live.log")
        live_store = RunStore(live_record, root=runs_root)
        live_store.update(status="running", stage="scanning", pid=multiprocessing.current_process().pid)
        active = active_run_records(runs_root)
        assert active["comment"]["pid"] == multiprocessing.current_process().pid
        live_ref = TrackedProcessRef(multiprocessing.current_process().pid)
        assert live_ref.poll() is None

        caffeinate_path = root / "caffeinate"
        caffeinate_path.touch()
        assert macos_sleep_guard_command(
            4321,
            platform_name="darwin",
            caffeinate_path=caffeinate_path,
        ) == [
            str(caffeinate_path),
            "-i",
            "-m",
            "-u",
            "-w",
            "4321",
        ]
        assert (
            macos_sleep_guard_command(
                4321,
                platform_name="linux",
                caffeinate_path=caffeinate_path,
            )
            is None
        )
        assert (
            macos_sleep_guard_command(
                0,
                platform_name="darwin",
                caffeinate_path=caffeinate_path,
            )
            is None
        )

    print("Runtime regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
