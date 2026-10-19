"""Discovery-only checks. This module never fetches chapter images or launches a browser."""
from __future__ import annotations
import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
from automation_runtime import atomic_write_json
from panelyra.services.reading_catalog import catalog, read_json
from panelyra.services.chapter_counts import _fetch
from panelyra.services.chapter_availability import parse_catalog
from panelyra.services.library_store import connect, db_path, transaction
from panelyra.services.worker_runtime import worker_lease, held_pid, is_engine, engine_environment

ROOT=Path(__file__).resolve().parents[2]

def _state(db):
    return {'jobs':[json.loads(r[0]) for r in db.execute('SELECT payload FROM discovery_jobs ORDER BY created,id')],
            'results':{r[0]:json.loads(r[1]) for r in db.execute('SELECT series,payload FROM discovery_results')}}

def state(root):
    with connect(db_path(root)) as db:
        return _state(db)

def mutate(root, callback):
    # Same transactional store as Library/progress/downloads. Old discovery.json
    # is retained untouched, not reimported into the user's chosen fresh history.
    with transaction(db_path(root)) as db:
        value=_state(db); before=copy.deepcopy(value); result=callback(value)
        old={j['id']:j for j in before['jobs']}
        for job in value['jobs']:
            if job != old.get(job['id']):
                db.execute('INSERT INTO discovery_jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           (job['id'],json.dumps(job),job.get('created_at',job['updated_at'])))
        removed=set(old)-{j['id'] for j in value['jobs']}
        db.executemany('DELETE FROM discovery_jobs WHERE id=?',[(key,) for key in removed])
        for key,result_value in value['results'].items():
            if result_value != before['results'].get(key):
                db.execute('INSERT INTO discovery_results VALUES(?,?,?) ON CONFLICT(series) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                           (key,json.dumps(result_value),time.time()))
        return result

def validate_url(url):
    p=urlsplit(url)
    import ipaddress
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise ValueError('Use a public HTTPS series landing-page URL.')
    if p.hostname in {'localhost','localhost.localdomain'} or '.' not in p.hostname:raise ValueError('Use a public source URL.')
    try:
        if not ipaddress.ip_address(p.hostname).is_global:raise ValueError('Private network URLs are not sources.')
    except ValueError as exc:
        if 'Private network' in str(exc):raise
    if p.hostname.endswith(('.local','.internal','.localhost')):raise ValueError('Private network URLs are not sources.')
    return url

def mapping(root, series, entry):
    from panelyra.services.library_store import connect, db_path
    with connect(db_path(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='reading_sources'").fetchone()
    saved=json.loads(row[0]) if row else {}
    manual=saved.get(series) or read_json(root/'data/updates/reading_sources.json').get(series)
    if manual:return manual
    urls={c['metadata'].get('series_url','').rstrip('/') for cs in entry['editions'].values() for c in cs.values() if c['verified']}
    urls.discard('')
    if not urls:
        legacy=read_json(root/'data/updates/series_sources.json').get(series,{})
        urls={str(u).rstrip('/') for u in legacy.get('series_urls',[])}
    urls={url for url in urls if not re.search(r'/(?:chapter|episode)[-/]\d',urlsplit(url).path) and urlsplit(url).path.rstrip('/').split('/')[-1] not in {'chapter','comics','webtoon','manga','series'}}
    if len(urls)==1:
        url=urls.pop()
        p=urlsplit(url)
        if p.path.rstrip('/').split('/')[-1] not in {'chapter','comics','webtoon','manga','series'} and not re.search(r'/(?:chapter|episode)[-/]\d',p.path):
            try:return {'url':validate_url(url),'language':entry['metadata'].get('language','unknown')}
            except ValueError:pass
    return None

def save_mapping(root, series, url, language):
    if series not in catalog(root):raise ValueError('Series not found.')
    validate_url(url)
    if language not in {'en','ko','unknown'}:raise ValueError('Invalid language.')
    # This is an explicit user mapping, never a title-similarity guess.
    from panelyra.services.library_store import transaction, db_path, canonical_source, event
    entry = catalog(root)[series]
    with transaction(db_path(root)) as db:
        old = db.execute('SELECT series_id FROM sources WHERE identity=? AND language=?', (canonical_source(url),language)).fetchone()
        if old and old[0] != entry['series_id']:
            raise ValueError('That source already belongs to another book. Resolve the identity conflict before changing it.')
        db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)', (canonical_source(url),language,entry['series_id']))
        row=db.execute("SELECT value FROM settings WHERE key='reading_sources'").fetchone()
        chosen=json.loads(row[0]) if row else {}
        chosen[series]={'url':url,'language':language}
        db.execute("INSERT INTO settings VALUES('reading_sources',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(chosen),))
        event(db, 'source-confirmed', {'series':entry['series_id'],'source':canonical_source(url),'language':language})

def parse_list(html, url):
    # Explicit labels are authoritative; URL tokens are identifiers, not chapter numbers.
    soup=BeautifulSoup(html,'html.parser');text=soup.get_text(' ',strip=True).lower()
    if any(x in text for x in ('just a moment','verify you are human','access denied','checking your browser')):
        return {'chapters':{},'complete':False,'status':'blocked','error':'This source needs browser verification. Open the source manually; no browser was launched.'}
    parsed=parse_catalog(html,url,'');entries={}
    scope=soup.select_one('#chapter-list,#chapterlist,#chapters,.listing-chapters_wrap,.chapters') or soup
    series_path=urlsplit(url).path.rstrip('/')
    for a in scope.select('a[href]'):
        label=a.get_text(' ',strip=True)
        match=re.search(r'\b(?:chapter|ch\.?|episode)\s*(\d+(?:\.\d+)?)\b',label,re.I)
        if not match:continue
        absolute=urljoin(url,a['href']);p=urlsplit(absolute)
        if p.hostname!=urlsplit(url).hostname or p.scheme!='https' or p.username or p.password:continue
        belongs=p.path.startswith(series_path+'/') or p.path.startswith(series_path+'-chapter-') or p.path.startswith(series_path+'-episode-') or (scope is not soup and p.path!=series_path) or ('weebcentral.com'==p.hostname and p.path.startswith('/chapters/'))
        if not belongs:continue
        n=str(float(match[1])).removesuffix('.0')
        if float(n)>=10000:continue
        wrapper=a.find_parent('li') or a
        locked=bool(wrapper.select_one('.lucide-lock,[data-locked="true"]')) or bool(re.search(r'\b(?:locked|unlock|premium|coins?)\b',wrapper.get_text(' ',strip=True),re.I))
        entries[n]={'number':n,'title':label,'url':absolute,'source_id':p.path,'status':'locked' if locked else 'public_link'}
    complete=bool(parsed.get('complete') and len(entries)==len(parsed.get('chapters',{})))
    return {'chapters':entries,'complete':complete,'status':'ok' if complete else 'partial' if entries else 'unverified','error':'' if entries else 'No trustworthy labelled chapter list was found. Check the series URL or source.'}

def check(root, series, entry, fetch=_fetch):
    source=mapping(root,series,entry)
    if not source:return {'series':series,'checked_at':time.time(),'status':'needs_source','error':'Choose a reliable series landing URL before checking.','chapters':[],'complete':False}
    try:
        result=parse_list(fetch(validate_url(source['url'])),source['url'])
    except Exception as exc:
        blocked=getattr(getattr(exc,'response',None),'status_code',None) in {403,429}
        return {'series':series,'source':source,'checked_at':time.time(),'status':'blocked' if blocked else 'failed','error':'Source blocked this check. Open the source manually or retry later.' if blocked else 'Source check failed: '+str(exc)[:180],'chapters':[],'complete':False}
    local={str(c['number']).removesuffix('.0') for cs in entry['editions'].values() for c in cs.values() if c['verified'] and c['healthy']}
    from panelyra.automation.download_queue import read_queue
    queued=set()
    for job in read_queue(root)['jobs']:
        if job['series_url'].rstrip('/')==source['url'].rstrip('/') and job['status'] in {'queued','running','interrupted','completed','partial'}:
            queued.update(n for n,c in job['chapters'].items() if c['status'] in {'pending','downloading','downloaded'})
    rows=[dict(c,availability='saved' if n in local else 'queued' if n in queued else c['status']) for n,c in result['chapters'].items()]
    return dict(result,series=series,source=source,checked_at=time.time(),chapters=rows,new_count=sum(c['availability']=='public_link' for c in rows))

def request(root, series=None):
    from panelyra.engine import policy
    if policy(root):raise ValueError('Background work is paused in Settings.')
    if series is not None and series not in catalog(root):raise ValueError('Series not found.')
    def add(value):
        for j in value['jobs']:
            if j['state'] in {'queued','running','interrupted'} and (j['series'] is None or j['series']==series):return j['id']
        identity=uuid.uuid4().hex;value['jobs'].append({'id':identity,'series':series,'state':'queued','updated_at':time.time(),'done_series':[]})
        # Never truncate pending jobs merely because many books were selected.
        finished=[j['id'] for j in value['jobs'] if j['state'] in {'finished','failed'}][:-50]
        value['jobs']=[j for j in value['jobs'] if j['id'] not in finished]
        return identity
    identity=mutate(root,add)
    return identity

def ensure_worker(root):
    from panelyra.engine import policy
    if not is_engine(root) or policy(root):return
    if held_pid(root/'data/updates/discovery-worker.lock'):return
    if not any(j['state'] in {'queued','running','interrupted'} for j in state(root)['jobs']):return
    (root/'data/updates').mkdir(parents=True,exist_ok=True)
    with (root/'data/updates/discovery-worker.log').open('a') as log:
        subprocess.Popen([sys.executable,'-m','panelyra.services.reading_updates','--root',str(root)],cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,stdin=subprocess.DEVNULL,env=engine_environment(root))

def run(root,fetch=_fetch):
    from panelyra.engine import policy
    base=root/'data/updates';base.mkdir(parents=True,exist_ok=True)
    with worker_lease(root,'discovery') as lease:
        while True:
            if policy(root):return
            def take(value):
                j=next((j for j in value['jobs'] if j['state'] in {'queued','running','interrupted'}),None)
                if j:j.update(state='running',updated_at=time.time(),pid=os.getpid())
                else:fcntl.flock(lease,fcntl.LOCK_UN)
                return dict(j) if j else None
            job=mutate(root,take)
            if not job:return
            finished=False
            try:
                entries=catalog(root)
                for key,entry in entries.items():
                    if policy(root):return
                    if job['series'] is not None and key!=job['series']:continue
                    if key in job.get('done_series',[]):continue
                    result=check(root,key,entry,fetch)
                    def publish(value):
                        previous=value['results'].get(key,{})
                        if result['status'] in {'ok','partial'}:result['last_successful_at']=result['checked_at']
                        else:result['last_successful_at']=previous.get('last_successful_at');result['previous_chapters']=previous.get('chapters',[])
                        value['results'][key]=result
                        live=next(j for j in value['jobs'] if j['id']==job['id'])
                        live.setdefault('done_series',[]).append(key)
                        live.update(updated_at=time.time())
                    mutate(root,publish)
                error=''
            except Exception as exc:error=str(exc)[:300]
            finally:
                # Interruptions (including SIGTERM's BaseException) retain the
                # current job plus per-series checkpoints; no completed refetch.
                if sys.exc_info()[0] is not None or policy(root):
                    mutate(root,lambda value:next(j for j in value['jobs'] if j['id']==job['id']).update(state='interrupted',updated_at=time.time()))
                else:
                    finished=True
            if finished:
                def finish(value):next(j for j in value['jobs'] if j['id']==job['id']).update(state='failed' if error else 'finished',error=error,updated_at=time.time())
                mutate(root,finish)

def scheduled(root=ROOT):
    from panelyra.engine import policy
    if policy(root):return
    # Legacy timer can request work, but cannot start an independent worker or
    # wake a closed engine. No timer or login service is installed here.
    from panelyra.services.worker_runtime import engine_pid
    if not engine_pid(root):return
    request(root)

def notify(root):
    prefs=read_json(root/'config/preferences.json')
    if prefs.get('mac_notifications'):
        count=sum(r.get('new_count',0) for r in state(root)['results'].values())
        fingerprint=hashlib.sha256(json.dumps({s:[c['url'] for c in r.get('chapters',[]) if c.get('availability')=='public_link'] for s,r in state(root)['results'].items()},sort_keys=True).encode()).hexdigest()
        path=root/'data/updates/notification.json'
        if count and read_json(path).get('fingerprint')!=fingerprint:
            subprocess.run(['osascript','-e',f'display notification "{count} chapters available. Review them in Panelyra." with title "Panelyra"'],capture_output=True)
            atomic_write_json(path,{'fingerprint':fingerprint})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=ROOT);args=parser.parse_args()
    from panelyra.services.worker_runtime import watch_owner, WorkerInterrupted
    import signal
    def interrupted(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN)
        raise WorkerInterrupted()
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    watcher=watch_owner(args.root,lambda:os.kill(os.getpid(),signal.SIGTERM))
    try:
        run(args.root);notify(args.root)
    except WorkerInterrupted:pass
    finally:watcher.set()
