"""Capture checkpoints in the shared database; JSON is a compatibility view only.

Only an explicitly started, browser-approved run is eligible for restart. Failed,
cancelled and waiting-for-review runs never silently retry or open a browser.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from panelyra.services.library_store import connect, transaction, db_path


class CaptureStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = db_path(self.root)

    def create(self, record):
        with transaction(self.path) as db:
            db.execute('INSERT OR IGNORE INTO capture_runs(id,record,status,updated) VALUES(?,?,?,?)',
                       (record['run_id'], json.dumps(record), record['status'], time.time()))

    def read(self, run_id):
        with connect(self.path) as db:
            row = db.execute('SELECT record FROM capture_runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, run_id, changes):
        with transaction(self.path) as db:
            row = db.execute('SELECT record FROM capture_runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise ValueError('Capture record is missing; it was not reset.')
            record = json.loads(row[0])
            # A late worker callback must not undo an explicit dashboard Stop.
            if record['status'] == 'stopped' and changes.get('status') != 'stopped':
                changes = {k: v for k, v in changes.items() if k == 'pid'}
            record.update(changes)
            record['updated_at'] = int(time.time())
            db.execute('UPDATE capture_runs SET record=?,status=?,updated=? WHERE id=?',
                       (json.dumps(record), record['status'], time.time(), run_id))
        return record

    def records(self, active_only=False):
        with connect(self.path) as db:
            query='SELECT record FROM capture_runs'
            if active_only:query+=" WHERE status IN ('starting','running','interrupted')"
            return [json.loads(row[0]) for row in db.execute(query+' ORDER BY updated DESC')]

    def command(self, run_id, command):
        # Never load arbitrary persisted executables on restart.
        expected = ['-m', 'panelyra.automation.managed_capture', '--root', str(self.root)]
        if command[1:5] != expected:
            raise ValueError('Only the managed local Capture command can be resumed.')
        with transaction(self.path) as db:
            db.execute('UPDATE capture_runs SET command=? WHERE id=?', (json.dumps(command), run_id))

    def checkpoint(self, run_id, stage, payload):
        with transaction(self.path) as db:
            row = db.execute('SELECT checkpoint FROM capture_runs WHERE id=?', (run_id,)).fetchone()
            checkpoint = json.loads(row[0]) if row else {}
            checkpoint.update(payload, stage=stage)
            db.execute('UPDATE capture_runs SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), run_id))
            if payload.get('chapter_index') and payload.get('url'):
                db.execute('''INSERT INTO capture_chapters VALUES(?,?,?,?,?,?)
                    ON CONFLICT(run_id,position) DO UPDATE SET url=excluded.url,state=excluded.state,
                    artifact=excluded.artifact,updated=excluded.updated''',
                    (run_id, int(payload['chapter_index']), payload['url'], stage,
                     payload.get('artifact', ''), time.time()))

    def resume(self, run_id):
        with connect(self.path) as db:
            row = db.execute('SELECT checkpoint FROM capture_runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else {}


def ensure_worker(root):
    """Engine-only restart, one unfinished chapter, no hot retries after failure."""
    from panelyra.services.worker_runtime import worker_pid, engine_environment, is_engine
    from panelyra.engine import policy
    if policy(root) or not is_engine(root) or worker_pid(root, 'capture'):
        return
    store = CaptureStore(root)
    for record in reversed(store.records(active_only=True)):
        run_id = record['run_id']
        # Reconcile a crashed worker without trusting a reused PID as ownership.
        if record['status'] in {'running', 'starting'} and time.time() - record['updated_at'] > 30:
            store.update(run_id, {'status': 'interrupted', 'pid': 0})
            record['status'] = 'interrupted'
        if record['status'] != 'interrupted' or not store.resume(run_id).get('approved'):
            continue
        with connect(store.path) as db:
            command = json.loads(db.execute('SELECT command FROM capture_runs WHERE id=?', (run_id,)).fetchone()[0])
        if not command:
            continue
        store.command(run_id, command)  # Recheck the fixed module/root before execution.
        command[0] = sys.executable
        environment = os.environ.copy()
        environment.update(engine_environment(root))
        environment['PANELYRA_RUN_RECORD'] = str(Path(root) / 'runs' / (run_id + '.json'))
        store.update(run_id, {'status': 'starting', 'stage': 'resuming', 'pid': 0})
        try:
            with open(record['log_path'], 'a') as log:
                process = subprocess.Popen(command, cwd=root, env=environment, stdout=log,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            store.update(run_id, {'pid': process.pid})
        except OSError as exc:
            store.update(run_id, {'status': 'failed', 'message': str(exc), 'stop_reason': str(exc)})
        return
