"""Transactional deliveries. Immutable manifests and mutable acknowledgements are separate.

Existing JSON is imported once, never overwritten. Damaged records are reported.
"""
from __future__ import annotations
import json
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from sync_library import OriginalLibrary, chapter_identity
from panelyra.services.library_store import connect, db_path, transaction

STATUSES={'queued','receiving','verifying','waiting','failed','paused','cancelled','ready'}
MAX_CHAPTERS=10000


class DeliveryStore:
    def __init__(self, app_dir: Path, library: OriginalLibrary | None = None):
        self.app_dir=Path(app_dir).resolve()
        self.root=self.app_dir/'data/tablet_companion/deliveries'
        self.root.mkdir(parents=True,exist_ok=True)
        self.db_path=db_path(self.app_dir)
        self.library=library or OriginalLibrary(self.app_dir)
        self._import_legacy()

    @staticmethod
    def _identity(identity):
        if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{32}',identity):raise ValueError('Invalid transfer identity')
        return identity

    @staticmethod
    def public(value):return {key:item for key,item in value.items() if key!='files'}

    def _local_path(self,value):
        path=Path(value).resolve()
        try:path.relative_to(self.app_dir)
        except ValueError:raise ValueError('Delivery file is outside the Panelyra library') from None
        return str(path)

    def _insert(self,db,value):
        identity=self._identity(value['transfer_id'])
        chapters=value['chapters'];transport=value.get('transport','pull')
        if transport not in ('pull','push') or value['status'] not in STATUSES:raise ValueError('Invalid delivery transport or state')
        if not isinstance(chapters,list) or not 1<=len(chapters)<=MAX_CHAPTERS:raise ValueError('Invalid chapter manifest')
        target=value.get('target_pin','')
        if transport=='push' and not re.fullmatch('[a-f0-9]{64}',target):raise ValueError('Invalid receiving tablet identity')
        acknowledged=set(value.get('acknowledged',[]));rows=[];seen=set()
        for position,original in enumerate(chapters):
            chapter=dict(original);series=str(chapter['series_key']);number=chapter_identity(str(chapter['chapter']))
            if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.() -]{0,199}',series):raise ValueError('Invalid series identity')
            if len(number)>40 or not re.fullmatch(r'\d+(?:\.\d+)?',number):raise ValueError('Invalid chapter identity')
            key=f'{series}/{number}'
            if key in seen:raise ValueError('Duplicate chapter in delivery manifest')
            seen.add(key);chapter['chapter']=number
            if not re.fullmatch('[a-f0-9]{64}',chapter['sha256']) or not re.fullmatch('[a-f0-9]{64}',chapter['cover_version']):raise ValueError('Invalid delivery file version')
            if type(chapter['size']) is not int or not 0<chapter['size']<=1_000_000_000:raise ValueError('Invalid delivery file size')
            chapter['download_url']=f'/api/v3/transfers/{identity}/chapters/{series}/{number}'
            chapter['cover_url']=f'/api/v3/transfers/{identity}/covers/{series}'
            rows.append((identity,series,number,position,json.dumps(chapter),self._local_path(value['files'][key]),
                         self._local_path(value['files']['cover/'+series]),int(key in acknowledged)))
        if acknowledged-seen:raise ValueError('Acknowledgement references an unknown chapter')
        if value['status']=='ready' and len(acknowledged)!=len(chapters):raise ValueError('Ready transfer lacks chapter acknowledgements')
        total=sum(c['size'] for c in chapters)
        created=float(value['created_at']);updated=float(value.get('updated_at',created))
        if not math.isfinite(created) or not math.isfinite(updated):raise ValueError('Invalid delivery timestamp')
        db.execute('INSERT INTO transfers VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (identity,transport,target,created,updated,value['status'],total,
                    total if value['status']=='ready' else min(total,max(0,int(value.get('received_bytes',0)))),max(0,int(value.get('bytes_per_second',0))),
                    max(0,int(value.get('eta_seconds',0))),str(value.get('error',''))[:300]))
        db.executemany('INSERT INTO transfer_chapters VALUES(?,?,?,?,?,?,?,?)',rows)

    def _import_legacy(self):
        with transaction(self.db_path) as db:
            if db.execute("SELECT 1 FROM settings WHERE key='delivery_json_imported'").fetchone():return
            for path in sorted(self.root.glob('*.json')):
                if not re.fullmatch('[a-f0-9]{32}\\.json',path.name):continue
                try:
                    if path.is_symlink() or path.stat().st_size>32_000_000:raise ValueError('Unsafe or oversized legacy delivery')
                    value=json.loads(path.read_text())
                    if value['transfer_id']!=path.stem:raise ValueError('Transfer identity differs from filename')
                    db.execute('SAVEPOINT legacy_record')
                    try:self._insert(db,value)
                    except BaseException:
                        db.execute('ROLLBACK TO legacy_record');raise
                    finally:db.execute('RELEASE legacy_record')
                except (OSError,ValueError,KeyError,TypeError,OverflowError,sqlite3.IntegrityError) as error:
                    db.execute('INSERT OR REPLACE INTO migration_issues VALUES(?,?,?,?)',('deliveries',path.name,str(error)[:300],time.time()))
            tablet=self.root/'tablet.json'
            if tablet.exists():
                try:
                    if tablet.is_symlink() or tablet.stat().st_size>4_000_000:raise ValueError('Unsafe or oversized tablet inventory')
                    saved=json.loads(tablet.read_text());self._validate_inventory(saved.get('inventory',{}))
                    db.execute("INSERT OR IGNORE INTO settings VALUES('tablet_presence',?)",(json.dumps(saved),))
                except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
                    db.execute('INSERT OR REPLACE INTO migration_issues VALUES(?,?,?,?)',('deliveries',tablet.name,str(error)[:300],time.time()))
            db.execute("INSERT INTO settings VALUES('delivery_json_imported',?)",(json.dumps({'at':time.time()}),))

    def issues(self):
        with connect(self.db_path) as db:return [dict(r) for r in db.execute("SELECT origin,error FROM migration_issues WHERE scope='deliveries' ORDER BY origin")]

    def _get(self,db,identity):
        self._identity(identity)
        row=db.execute('SELECT * FROM transfers WHERE id=?',(identity,)).fetchone()
        if row is None:raise ValueError('Transfer not found')
        value={'transfer_id':row['id'],'transport':row['transport'],'target_pin':row['target_pin'],'created_at':row['created'],'updated_at':row['updated'],
               'status':row['status'],'total_bytes':row['total_bytes'],'received_bytes':row['received_bytes'],'bytes_per_second':row['bytes_per_second'],
               'eta_seconds':row['eta_seconds'],'error':row['error'],'chapters':[],'files':{},'acknowledged':[]}
        for item in db.execute('SELECT * FROM transfer_chapters WHERE transfer_id=? ORDER BY position',(identity,)):
            chapter=json.loads(item['manifest']);key=f"{item['series_key']}/{item['chapter']}"
            value['chapters'].append(chapter);value['files'][key]=item['file_path'];value['files']['cover/'+item['series_key']]=item['cover_path']
            if item['acknowledged']:value['acknowledged'].append(key)
        return value

    def get(self,identity):
        with connect(self.db_path) as db:
            db.execute('BEGIN');return self._get(db,identity)

    def status(self,identity):
        self._identity(identity)
        with connect(self.db_path) as db:
            row=db.execute('SELECT status FROM transfers WHERE id=?',(identity,)).fetchone()
            if row is None:raise ValueError('Transfer not found')
            return row[0]

    def list(self,pending=False,transport=None,states=None):
        clauses=[];params=[]
        if pending:clauses.append("status NOT IN ('ready','cancelled')")
        if transport:clauses.append('transport=?');params.append(transport)
        if states:
            clauses.append('status IN ('+','.join('?' for _ in states)+')');params.extend(states)
        with connect(self.db_path) as db:
            db.execute('BEGIN')
            query='SELECT id FROM transfers'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY created,id'
            return [self.public(self._get(db,row[0])) for row in db.execute(query,params).fetchall()]

    @staticmethod
    def _validate_inventory(inventory):
        if not isinstance(inventory,dict) or len(inventory)>10000:raise ValueError('Invalid tablet inventory')
        if any(not isinstance(k,str) or len(k)>250 or not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for k,v in inventory.items()):raise ValueError('Invalid tablet inventory version')

    def tablet(self,body=None):
        with transaction(self.db_path) as db:
            row=db.execute("SELECT value FROM settings WHERE key='tablet_presence'").fetchone();saved=json.loads(row[0]) if row else {}
            if body is not None:
                if 'inventory' in body:self._validate_inventory(body['inventory']);saved['inventory']=body['inventory']
                saved.update(updated_at=time.time(),device=str(body.get('device','Huawei'))[:100],version=str(body.get('version',''))[:20])
                db.execute("INSERT INTO settings VALUES('tablet_presence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(saved),))
            return saved

    def create(self,selection,missing=False,*,transport='pull',target_pin=''):
        if not isinstance(selection,list) or not 1<=len(selection)<=MAX_CHAPTERS:raise ValueError('Select 1–10000 chapters')
        if transport not in ('pull','push'):raise ValueError('Invalid delivery transport')
        if transport=='push' and not re.fullmatch('[a-f0-9]{64}',target_pin):raise ValueError('Connect to the receiving tablet first')
        catalog=self.library.catalog();lookup={(c['series_key'],c['chapter']):c for c in catalog['chapters']}
        lookup.update({(c['library_key'],c['chapter']):c for c in catalog['chapters'] if c.get('library_key')})
        inventory=self.tablet().get('inventory',{}) if missing else {};identity=uuid.uuid4().hex;chapters=[];files={};seen=set()
        for item in selection:
            key=(str(item['series_key']),chapter_identity(str(item['chapter'])))
            if key not in lookup:raise ValueError(f'{key[0]} chapter {key[1]} is not valid or complete')
            chapter=dict(lookup[key]);key=(chapter['series_key'],chapter['chapter'])
            if key in seen:continue
            seen.add(key)
            if inventory.get('/'.join(key))==chapter['sha256']:
                if inventory.get('cover/'+key[0])==chapter['cover_version']:continue
                chapter['cover_only']=True
            files['/'.join(key)]=str(self.library.chapter_file(*key));cover=self.library.cover_file(key[0])
            snapshot=self.root/'covers'/f"{chapter['cover_version']}.cover"
            if not snapshot.exists():
                snapshot.parent.mkdir(parents=True,exist_ok=True);temporary=snapshot.with_name(snapshot.name+f'.{uuid.uuid4().hex}.partial')
                with temporary.open('wb') as out:out.write(cover.read_bytes());out.flush();os.fsync(out.fileno())
                os.replace(temporary,snapshot)
            files['cover/'+key[0]]=str(snapshot);chapters.append(chapter)
        if not chapters:raise ValueError('All selected chapters are already verified on the tablet')
        value={'transfer_id':identity,'transport':transport,'target_pin':target_pin,'created_at':time.time(),'status':'queued','chapters':chapters,'files':files,'acknowledged':[]}
        with transaction(self.db_path) as db:self._insert(db,value)
        return self.public(self.get(identity))

    def update(self,identity,action,payload,*,summary=False):
        self._identity(identity)
        if action not in ('pause','resume','cancel','progress','ack'):raise ValueError('Invalid action')
        with transaction(self.db_path) as db:
            current=db.execute('SELECT * FROM transfers WHERE id=?',(identity,)).fetchone()
            if current is None:raise ValueError('Transfer not found')
            if current['status']!='cancelled':
                if action in ('pause','resume','cancel') and current['status']!='ready':
                    status={'pause':'paused','resume':'queued','cancel':'cancelled'}[action]
                    db.execute('UPDATE transfers SET status=?,bytes_per_second=0,eta_seconds=0,error=? WHERE id=?',(status,'' if action=='resume' else current['error'],identity))
                elif action=='ack':
                    series=str(payload.get('series_key'));number=chapter_identity(str(payload.get('chapter','')))
                    item=db.execute('SELECT manifest FROM transfer_chapters WHERE transfer_id=? AND series_key=? AND chapter=?',(identity,series,number)).fetchone()
                    if item is None or json.loads(item[0])['sha256']!=payload.get('sha256'):raise ValueError('Acknowledgement version mismatch')
                    db.execute('UPDATE transfer_chapters SET acknowledged=1 WHERE transfer_id=? AND series_key=? AND chapter=?',(identity,series,number))
                    if not db.execute('SELECT 1 FROM transfer_chapters WHERE transfer_id=? AND acknowledged=0 LIMIT 1',(identity,)).fetchone():
                        db.execute("UPDATE transfers SET status='ready',received_bytes=total_bytes,bytes_per_second=0,eta_seconds=0,error='' WHERE id=?",(identity,))
                elif action=='progress' and current['status'] not in ('paused','ready'):
                    status=payload.get('status','receiving')
                    if status not in ('receiving','verifying','waiting','failed'):raise ValueError('Invalid progress stage')
                    db.execute('UPDATE transfers SET status=?,received_bytes=?,bytes_per_second=?,eta_seconds=?,error=? WHERE id=?',
                               (status,min(current['total_bytes'],max(0,int(payload.get('received_bytes',current['received_bytes'])))),max(0,int(payload.get('bytes_per_second',0))),
                                max(0,int(payload.get('eta_seconds',0))),str(payload.get('error',''))[:300],identity))
                db.execute('UPDATE transfers SET updated=? WHERE id=?',(time.time(),identity))
            if summary:
                value=dict(db.execute('SELECT * FROM transfers WHERE id=?',(identity,)).fetchone())
                value['transfer_id']=value.pop('id');value['created_at']=value.pop('created');value['updated_at']=value.pop('updated')
                return value
            return self.public(self._get(db,identity))

    def file(self,identity,series,chapter=None):
        self._identity(identity)
        with connect(self.db_path) as db:
            query='SELECT c.*,t.status FROM transfer_chapters c JOIN transfers t ON t.id=c.transfer_id WHERE t.id=? AND c.series_key=?'
            params=[identity,series]
            if chapter is not None:query+=' AND c.chapter=?';params.append(chapter_identity(str(chapter)))
            row=db.execute(query+' ORDER BY c.position LIMIT 1',params).fetchone()
        if row is None:raise ValueError('Delivery chapter not found')
        if row['status'] in ('paused','cancelled'):raise ValueError('Transfer is '+row['status'])
        item=json.loads(row['manifest']);source=Path(self._local_path(row['file_path'] if chapter is not None else row['cover_path']))
        digest=item['sha256'] if chapter is not None else item['cover_version']
        if self.library.verified_digest(source)!=digest:raise ValueError(f'{series} chapter {chapter}: file changed; send its new version')
        return source,digest
