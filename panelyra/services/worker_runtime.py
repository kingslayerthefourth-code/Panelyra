"""Engine-owned worker leases and cooperative owner-loss interruption.

PIDs in stale files are never authority to signal a process. Both a held lease and
the expected module/root in the live command are required. No global process kill.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

WORKERS = {
    'capture': ('data/engine/capture-worker.lock', 'panelyra.automation.managed_capture'),
    'discovery': ('data/updates/discovery-worker.lock', 'panelyra.services.reading_updates'),
    'downloads': ('data/manga_backend/queue-worker.lock', 'panelyra.automation.download_queue'),
    'backend': ('data/engine/backend-worker.lock', 'panelyra.automation.manga_backend'),
    'companion': ('data/engine/companion-worker.lock', 'panelyra.automation.companion_service'),
}


def held_pid(path):
    try:
        with Path(path).open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return None
            except BlockingIOError:
                value = json.loads(handle.read())
                pid = int(value['pid'] if isinstance(value, dict) else value)
                return pid if pid > 0 else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def engine_pid(root):
    return held_pid(Path(root) / 'data/engine/owner.lock')


def engine_environment(root):
    root = Path(root).resolve()
    owner = engine_pid(root)
    if not owner:
        raise ValueError('Open Panelyra with its launcher before starting work.')
    return dict(os.environ, PANELYRA_ENGINE_ROOT=str(root), PANELYRA_ENGINE_PID=str(owner))


def is_engine(root):
    return engine_pid(root) == os.getpid()


def worker_pid(root, kind):
    relative, module = WORKERS[kind]
    pid = held_pid(Path(root) / relative)
    if pid is None:
        return None
    try:
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True)
        if module in command and any(str(path) in command for path in (Path(root).absolute(),Path(root).resolve())):
            return pid
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def signal_workers(root, signum=signal.SIGTERM):
    signalled = []
    for kind in WORKERS:
        pid = worker_pid(root, kind)
        if pid:
            try:
                os.kill(pid, signum)
                # A dashboard-paused Capture may be SIGSTOPped. Let it finalize.
                if signum == signal.SIGTERM:
                    os.kill(pid, signal.SIGCONT)
                signalled.append(pid)
            except ProcessLookupError:
                pass
    return signalled


def watch_owner(root, stop):
    """A worker also stops after an engine crash, not just a clean engine quit."""
    expected = os.environ.get('PANELYRA_ENGINE_PID', '')
    done = threading.Event()
    if not expected:
        return done  # In-process/offline tests and explicit legacy CLI stay separate.
    expected = int(expected)
    queue_parent=os.environ.get('PANELYRA_QUEUE_PID','')
    def watch():
        while not done.wait(.25):
            parent_lost=queue_parent and held_pid(Path(root)/WORKERS['downloads'][0]) != int(queue_parent)
            if (engine_pid(root) != expected or parent_lost) and not done.is_set():
                stop()
                return
    threading.Thread(target=watch, name='panelyra-owner-watch', daemon=True).start()
    return done


class WorkerInterrupted(BaseException):
    """Not a failed chapter, so Capture's retry/fallback handlers must not catch it."""


@contextmanager
def worker_lease(root, kind):
    relative, _ = WORKERS[kind]
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'Panelyra {kind} worker is already running.')
        lease.seek(0); lease.truncate(); lease.write(str(os.getpid())); lease.flush()
        try:
            yield lease
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


@contextmanager
def interruptible_worker(root, kind):
    if not engine_pid(root):
        raise ValueError('The Panelyra engine is not running. Open its launcher and retry.')
    from panelyra.engine import policy
    if policy(root):
        raise ValueError('Background work is paused in Settings.')
    previous = {}
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN)
        raise WorkerInterrupted('Interrupted by pause, quit or engine shutdown; completed work was retained.')
    with worker_lease(root, kind):
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        watcher = watch_owner(root, lambda: os.kill(os.getpid(), signal.SIGTERM))
        try:
            yield
        finally:
            watcher.set()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
