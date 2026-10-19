"""Explicit local process owner. No scheduler, login agent, AI or paid service.

Normal mode owns the dashboard until Quit/terminal close. --background-batch is
opt-in, processes the current queue, and exits when that batch is no longer active.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from panelyra.services.library_store import connect, transaction, db_path, LibraryStore
from panelyra.services.worker_runtime import WORKERS, worker_pid


def policy(root):
    with connect(db_path(root)) as db:
        row = db.execute("SELECT value FROM settings WHERE key='background_paused'").fetchone()
    return bool(json.loads(row[0])) if row else False


def pause(root, paused=True):
    with transaction(db_path(root)) as db:
        db.execute("INSERT INTO settings VALUES('background_paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(paused),))
    from panelyra.automation.download_queue import set_paused
    set_paused(root, paused)


def stop_workers(root, stopping):
    """One graceful signal, bounded drain, then revalidated forced stop if stuck."""
    live={pid for kind in WORKERS for pid in [worker_pid(root,kind)] if pid}
    for pid in live:
        try:
            if pid not in stopping:
                os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT)
                stopping[pid]=time.monotonic()
            elif time.monotonic()-stopping[pid]>10:
                os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass
    for pid in set(stopping)-live:stopping.pop(pid,None)
    return bool(live)


def batch_active(root):
    from panelyra.automation.download_queue import read_queue, ACTIVE
    from panelyra.services.reading_updates import state
    if policy(root):return False
    downloads=read_queue(root)
    return (not downloads['paused'] and any(j['status'] in ACTIVE for j in downloads['jobs'])) or any(
        j['state'] in {'queued','running','interrupted'} for j in state(root)['jobs']) or bool(worker_pid(root,'capture'))


def run(root, port=8517, background=False):
    root=Path(root).resolve(); base=root/'data/engine';base.mkdir(parents=True,exist_ok=True)
    lease=(base/'owner.lock').open('a+')
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Panelyra engine is already running. Open the existing dashboard.')
    lease.seek(0);lease.truncate();lease.write(str(os.getpid()));lease.flush()
    stopped=False
    def stop(signum, frame):
        nonlocal stopped
        stopped=True
    previous={sig:signal.signal(sig,stop) for sig in (signal.SIGTERM,signal.SIGINT)}
    child=None;sender=None;stopping={}
    try:
        LibraryStore(root).recover()
        from tablet_delivery import DeliveryStore
        DeliveryStore(root)  # Migrate durable manifests even while transfers stay paused.
        from panelyra.automation.download_queue import ensure_worker, read_queue, ACTIVE
        from panelyra.services.reading_updates import ensure_worker as ensure_updates
        from panelyra.services.companion_runtime import ensure_worker as ensure_companion
        from panelyra.services.capture_store import ensure_worker as ensure_capture
        from tablet_push import PushWorker
        sender=PushWorker(root)
        if not background:
            child=subprocess.Popen([sys.executable,'-m','streamlit','run','panelyra/app.py','--server.address','127.0.0.1','--server.port',str(port),'--server.headless','true','--browser.gatherUsageStats','false'],cwd=root,start_new_session=True)
        while not stopped and (child is None or child.poll() is None):
            if policy(root):
                sender.stop()
                stop_workers(root,stopping)
            else:
                stopping.clear()
                ensure_worker(root)
                ensure_updates(root)
                ensure_capture(root)
                if not background:ensure_companion(root)
                sender.start()
            if background and not batch_active(root):
                # A separate explicit invocation is required for future work.
                if policy(root) or not sender.thread or not sender.thread.is_alive():
                    break
            time.sleep(1)
    finally:
        if sender:sender.stop()
        while stop_workers(root,stopping):
            time.sleep(.1)
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        fcntl.flock(lease,fcntl.LOCK_UN);lease.close()
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--port',type=int,default=8517)
    p.add_argument('--background-batch',action='store_true');args=p.parse_args();run(args.root,args.port,args.background_batch)
