"""Durable, single-worker chapter queue. UI sessions are not process owners."""
from __future__ import annotations
import argparse
import copy
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from panelyra.services.download_identity import expand_chapters, existing_chapters, source_key
from panelyra.services.library_store import connect, db_path, transaction as db_transaction
from panelyra.services.chapter_availability import chapter_status

SUCCESS = {'downloaded','already_saved'}
ACTIVE = {'queued','running','interrupted'}
_STOP = False


def base_dir(root: Path) -> Path:
    return root/'data'/'manga_backend'


def read_queue(root: Path) -> dict:
    with connect(db_path(root)) as db:
        return _read_db(db)


def _read_db(db):
    row = db.execute("SELECT value FROM settings WHERE key='downloads_paused'").fetchone()
    failures = db.execute("SELECT value FROM settings WHERE key='source_failures'").fetchone()
    return {'version': 2, 'paused': bool(json.loads(row[0])) if row else False, 'source_failures':json.loads(failures[0]) if failures else {},
            'jobs': [json.loads(r[0]) for r in db.execute('SELECT payload FROM jobs ORDER BY updated,id')]}


def _save(root: Path, state: dict):
    with db_transaction(db_path(root)) as db:
        _save_db(db, state)


def _save_db(db, state):
    db.execute("INSERT INTO settings VALUES('downloads_paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(state['paused']),))
    db.execute("INSERT INTO settings VALUES('source_failures',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(state.get('source_failures',{})),))
    for job in state['jobs']:
        db.execute('INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                   (job['id'], json.dumps(job), job.get('created_at', 0)))


@contextmanager
def transaction(root: Path):
    base_dir(root).mkdir(parents=True,exist_ok=True)
    with db_transaction(db_path(root)) as db:
        state = _read_db(db)
        yield state
        _save_db(db, state)


def job_dir(root: Path, job: dict) -> Path:
    if len(job['id']) != 32 or any(c not in '0123456789abcdef' for c in job['id']):
        raise ValueError('Invalid queue job ID')
    return base_dir(root)/('run-queue-'+job['id'])


def enqueue(root: Path, series_url: str, chapters: str, title: str='', source: str='', catalog: dict | None=None, *, cover: str='') -> dict:
    from panelyra.automation.manga_backend import build_command
    build_command(series_url,chapters,root,browser_consent=True)
    requested = expand_chapters(chapters)
    existing = existing_chapters(root/'manhwa',series_url,title)
    with transaction(root) as state:
        queued = set()
        for other in state['jobs']:
            if other.get('language','en') == 'en' and source_key(other['series_url']) == source_key(series_url):
                queued.update(n for n,c in other['chapters'].items() if (other['status'] in ACTIVE and c['status'] in {'pending','downloading'}) or (c['status']=='downloaded' and _valid_archive(job_dir(root,other),n)))
        remaining = [n for n in requested if n not in queued and n not in existing]
        if not remaining:
            raise ValueError('These chapters are already in Library or the download queue. Nothing was downloaded again.')
        entries = {}
        for n in requested:
            info = chapter_status(catalog or {},n)
            status = 'already_saved' if n in existing else 'already_queued' if n in queued else info['status'] if info['status'] in {'locked','missing'} else 'pending'
            entries[n] = {'status':status,'source_status':info['status'],'url':info.get('url',''),'attempts':0,'error':''}
        job = {'id':uuid.uuid4().hex,'series_url':series_url,'title':title,'source':source,'cover':cover,'language':'en','created_at':time.time(),'updated_at':time.time(),'status':'queued','cancel_requested':False,'chapters':entries}
        if not any(c['status']=='pending' for c in entries.values()):
            raise ValueError('The remaining requested chapters are marked locked or missing by this source. Nothing was queued.')
        state['jobs'].append(job)
        staging = job_dir(root,job);staging.mkdir(parents=True)
        from automation_runtime import atomic_write_json
        atomic_write_json(staging/'request.json',{'series_url':series_url,'title':title,'source':source,'cover':cover,'chapters':','.join(n for n,c in entries.items() if c['status']=='pending'),'language':'en','queue_job_id':job['id'],'chapter_urls':{n:c['url'] for n,c in entries.items() if c['url']}})
        return copy.deepcopy(job)


def worker_running(root: Path) -> bool:
    path = base_dir(root)/'queue-worker.lock'
    if not path.exists():
        return False
    with path.open('r') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle,fcntl.LOCK_UN)
        return False


def ensure_worker(root: Path):
    # Serialize the idle-exit/start handoff with queue edits, so an enqueue cannot
    # be lost between the old worker's last read and releasing its lease.
    from panelyra.engine import policy
    from panelyra.services.worker_runtime import is_engine, engine_environment
    if not is_engine(root) or policy(root):
        return
    with transaction(root) as state:
        if state['paused'] or worker_running(root) or not any(j['status'] in ACTIVE for j in state['jobs']):
            return
        with (base_dir(root)/'queue-worker.log').open('a') as log:
            subprocess.Popen([sys.executable,'-m','panelyra.automation.download_queue','--root',str(root),'--worker'],cwd=root,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,env=engine_environment(root))


def set_paused(root: Path, paused: bool):
    with transaction(root) as state:
        state['paused']=paused


def cancel_job(root: Path, identity: str):
    with transaction(root) as state:
        job = next(j for j in state['jobs'] if j['id']==identity)
        job['cancel_requested']=True
        if job['status'] != 'running':
            job['status']='cancelled'
        job['updated_at']=time.time()


def retry_job(root: Path, identity: str):
    with transaction(root) as state:
        job=next(j for j in state['jobs'] if j['id']==identity)
        if job['status']=='running':
            raise ValueError('Stop the running job before retrying.')
        retryable = [c for c in job['chapters'].values() if c['status'] in {'failed','pending','downloading','cancelled'}]
        if not retryable:
            raise ValueError('No failed or interrupted chapters to retry. Locked and missing chapters require a fresh availability check.')
        for chapter in retryable:
            chapter['status']='pending';chapter['error']=''
        job['cancel_requested']=False;job['status']='queued';job['updated_at']=time.time()
        state.setdefault('source_failures',{})[source_key(job['series_url'])]=0


def _update(root: Path, identity: str, number: str, **values):
    with transaction(root) as state:
        job=next(j for j in state['jobs'] if j['id']==identity)
        chapter = job['chapters'][number]
        if values.get('status') in {'downloaded', 'failed'} and chapter.get('started_at'):
            values.setdefault('duration_seconds', max(0, time.time() - chapter['started_at']))
        chapter.update(values);job['updated_at']=time.time()


def _cancelled(root: Path, identity: str) -> bool:
    return _STOP or next(j for j in read_queue(root)['jobs'] if j['id']==identity).get('cancel_requested',False)


def execute_chapter(root: Path, job: dict, number: str, staging: Path, cancelled) -> int:
    if cancelled():raise InterruptedError('Stopped before starting the chapter; queue retained.')
    with (staging/'download.log').open('a') as log:
        log.write(f'\nChapter {number} — attempt {job["chapters"][number]["attempts"] + 1}\n');log.flush()
        process=subprocess.Popen([sys.executable,'-m','panelyra.automation.manga_backend','--series-url',job['series_url'],'--chapters',number,'--staging',str(staging),'--allow-browser'],cwd=root,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,env=dict(os.environ,PANELYRA_QUEUE_PID=str(os.getpid())))
        started=time.monotonic()
        try:
            while process.poll() is None:
                # Inactivity budget, not a total-batch deadline. Browser process is
                # recycled every chapter, with one chapter/four image requests.
                changed = max((p.stat().st_mtime for p in staging.rglob('*') if p.is_file()), default=time.time())
                if cancelled():
                    raise InterruptedError('Stopped; completed chapter archives were retained.')
                if time.time()-changed > 900:
                    raise TimeoutError('Source produced no progress for 15 minutes; resume after checking it.')
                time.sleep(.3)
            if cancelled():raise InterruptedError('Stopped; completed chapter archives were retained.')
            return process.returncode
        finally:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait()


def _valid_archive(staging: Path, number: str) -> bool:
    from panelyra.automation.manga_backend import archive_pages
    path=staging/(number+'.cbz')
    if not path.is_file():return False
    try:
        return bool(archive_pages(path))
    except Exception:
        return False


def _finish_status(job: dict):
    if job.get('status') == 'paused':
        return
    statuses={c['status'] for c in job['chapters'].values()}
    if job.get('cancel_requested'):job['status']='cancelled'
    elif statuses & {'pending','downloading'}:job['status']='queued'
    elif statuses <= SUCCESS | {'already_queued'}:job['status']='completed'
    elif statuses & SUCCESS:job['status']='partial'
    else:job['status']='failed'
    job['updated_at']=time.time()


def run_worker(root: Path, runner=execute_chapter):
    global _STOP
    _STOP=False
    base_dir(root).mkdir(parents=True,exist_ok=True)
    lease=(base_dir(root)/'queue-worker.lock').open('a+')
    watcher=None
    try:
        try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        lease.seek(0);lease.truncate();lease.write(str(os.getpid()));lease.flush()
        from panelyra.services.worker_runtime import watch_owner
        watcher=watch_owner(root,lambda:_stop(None,None))
        with transaction(root) as state:
            for job in state['jobs']:
                if job['status']=='running':
                    job['status']='cancelled' if job.get('cancel_requested') else 'queued'
                    for chapter in job['chapters'].values():
                        if chapter['status']=='downloading':chapter['status']='pending'
        while not _STOP:
            if runner is execute_chapter:
                from automation_runtime import browser_lock_owner
                from panelyra.services.worker_runtime import worker_pid
                if worker_pid(root, 'capture') or browser_lock_owner():
                    # Resource contention is not a source failure. Yield the browser
                    # before claiming a chapter, without charging retries/breakers.
                    if read_queue(root)['paused']:
                        return
                    time.sleep(.5)
                    continue
            with transaction(root) as state:
                job=next((j for j in state['jobs'] if j['status'] in ACTIVE and not j.get('cancel_requested')),None)
                if state['paused'] or job is None:
                    fcntl.flock(lease,fcntl.LOCK_UN)
                    return
                number=next((n for n,c in job['chapters'].items() if c['status']=='pending'),None)
                if number is None:
                    _finish_status(job);continue
                job['status']='running'
                job['chapters'][number].update(status='downloading', started_at=time.time(), stage='Preparing chapter')
                snapshot=copy.deepcopy(job)
            staging=job_dir(root,snapshot)
            try:
                existing=existing_chapters(root/'manhwa',snapshot['series_url'],snapshot.get('title',''))
                if number in existing:
                    _update(root,snapshot['id'],number,status='already_saved',error='');continue
                if _valid_archive(staging,number):
                    _publish(root,snapshot,number,staging)
                    _update(root,snapshot['id'],number,status='downloaded',error='');continue
                path=staging/(number+'.cbz')
                if path.exists():
                    preserved=staging/'incomplete';preserved.mkdir(exist_ok=True)
                    os.rename(path,preserved/(f'{number}-{time.time_ns()}.cbz'))
                _update(root,snapshot['id'],number,attempts=snapshot['chapters'][number]['attempts']+1)
                _update(root,snapshot['id'],number,stage='Receiving pages')
                code=runner(root,snapshot,number,staging,lambda:_cancelled(root,snapshot['id']))
                if code == 75:
                    _update(root,snapshot['id'],number,status='pending',
                            attempts=snapshot['chapters'][number]['attempts'],error='Waiting for the shared browser.')
                    time.sleep(.5)
                    continue
                _update(root,snapshot['id'],number,stage='Checking archive')
                valid=_valid_archive(staging,number)
                if valid:
                    _update(root,snapshot['id'],number,stage='Adding to Reader')
                    _publish(root,snapshot,number,staging)
                _update(root,snapshot['id'],number,status='downloaded' if valid else 'failed',error='' if valid else f'No valid chapter archive was produced (exit {code}).')
            except InterruptedError as exc:
                # A process can finish a good archive just as pause/cancel arrives.
                # Publish that verified work; interruption is not a source failure.
                saved=False;message=str(exc)
                if _valid_archive(staging,number):
                    try:
                        _publish(root,snapshot,number,staging);saved=True
                    except Exception as publication_error:
                        message=f'{exc} Archive retained; publication pending: {publication_error}'
                _update(root,snapshot['id'],number,status='downloaded' if saved else 'pending',error='' if saved else message)
                with transaction(root) as state:
                    live=next(j for j in state['jobs'] if j['id']==snapshot['id']);live['status']='cancelled' if live['cancel_requested'] else 'interrupted'
                return
            except Exception as exc:
                _update(root,snapshot['id'],number,status='failed',error=str(exc)[:500])
            finally:
                with transaction(root) as state:
                    live=next(j for j in state['jobs'] if j['id']==snapshot['id'])
                    source = source_key(live['series_url'])
                    # Global source circuit breaker, including overlapping jobs.
                    # Queue transaction already owns the write lock, so carry the
                    # counter in the queue state and persist it in _save_db below.
                    counts=state.setdefault('source_failures',{})
                    status=live['chapters'][number]['status']
                    if status in SUCCESS:counts[source]=0
                    elif status=='failed':counts[source]=counts.get(source,0)+1
                    if counts.get(source,0)>=3:
                        for other in state['jobs']:
                            if source_key(other['series_url'])==source and other['status'] in ACTIVE:
                                other['status']='paused'
                                other['error']='Three consecutive chapter attempts failed on this source. Check access or layout, then Retry. No source was switched.'
                    if live['status'] != 'interrupted':_finish_status(live)
    finally:
        if watcher:watcher.set()
        fcntl.flock(lease,fcntl.LOCK_UN);lease.close()


def _publish(root, job, number, staging):
    from panelyra.automation.manga_backend import import_reviewed_archive
    from panelyra.services.library_store import canonical_source, stable_id
    chapter_url = job['chapters'][number].get('url')
    if not chapter_url:
        raise ValueError('Needs confirmation: this source did not provide a chapter URL. Archive retained in staging.')
    slug = 'book-'+stable_id(canonical_source(job['series_url']), job.get('language','en'))[:20]
    return import_reviewed_archive(staging/(number+'.cbz'), job['series_url'], chapter_url, slug,
                                   output_dir=root/'manhwa', history_file=root/'data/updates/downloaded_chapters.json', title=job.get('title',''), verified_number=number)


def _stop(signum, frame):
    global _STOP
    _STOP=True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--worker',action='store_true',required=True);args=parser.parse_args()
    signal.signal(signal.SIGTERM,_stop);signal.signal(signal.SIGINT,_stop)
    run_worker(args.root)
