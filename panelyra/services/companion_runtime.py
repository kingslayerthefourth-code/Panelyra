"""An explicit, session-scoped request for the optional older LAN companion."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from panelyra.services.library_store import connect,db_path,transaction
from panelyra.services.worker_runtime import engine_pid,is_engine,engine_environment,held_pid,WORKERS


def state(root):
    with connect(db_path(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='companion_request'").fetchone()
        return json.loads(row[0]) if row else {}


def record(root,**fields):
    with transaction(db_path(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='companion_request'").fetchone()
        value=json.loads(row[0]) if row else {};value.update(fields,updated_at=time.time())
        db.execute("INSERT INTO settings VALUES('companion_request',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(value),))


def request(root):
    from panelyra.engine import policy
    if policy(root):raise ValueError('Background work is paused. Resume it in Settings to enable the older tablet connection.')
    owner=engine_pid(root)
    if not owner:raise ValueError('Open Panelyra with its launcher before connecting.')
    record(root,owner=owner,status='queued',error='')


def ensure_worker(root):
    from panelyra.engine import policy
    if not is_engine(root) or policy(root):return
    value=state(root)
    if value.get('owner')!=os.getpid() or value.get('status') not in ('queued','starting','running'):return
    if held_pid(Path(root)/WORKERS['companion'][0]):return
    if value['status'] in ('starting','running'):
        if time.time()-value.get('updated_at',0)<10:return
        record(root,status='failed',error='The companion stopped. Use Start / reconnect to retry.');return
    base=Path(root)/'data/tablet_companion';base.mkdir(parents=True,exist_ok=True)
    record(root,status='starting',error='')
    try:
        with (base/'companion.log').open('a') as log:
            subprocess.Popen([sys.executable,'-m','panelyra.automation.companion_service','--root',str(root)],cwd=root,
                             env=engine_environment(root),stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    except OSError as error:record(root,status='failed',error=str(error)[:200])
