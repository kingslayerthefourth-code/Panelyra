"""Local series/reader HTTP interface, with explicit public serialization."""
from __future__ import annotations
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlencode,parse_qs,urlsplit,quote
from automation_runtime import atomic_write_json
from panelyra.services.reading_catalog import catalog,read_json,sort_key,image_size
from panelyra.services import reading_updates as updates

WEB=Path(__file__).resolve().parents[1]/'web'

class ReadingRoutes:
    def series_url(self,series):
        return self.base_url+'/series?'+urlencode({'series':series,'reader_token':self.reader_token})

    def page_html(self,view,series='',chapter='',edition='Original'):
        payload={'view':view,'series':series.removesuffix('-clean'),'chapter':chapter,'edition':edition,'token':self.reader_token,'library':self.dashboard_url+'?page=Library','downloads':self.dashboard_url+'?page=Downloads'}
        return (WEB/'reading.html').read_text().replace('__STATE__',json.dumps(payload).replace('<','\\u003c'))

    def public_series(self,key):
        entry=catalog(self.app_dir).get(key)
        if not entry:raise ValueError('Series not found.')
        records={(p.edition,p.chapter):p.__dict__ for p in self.progress.all() if p.series==key}
        editions={}
        for edition,chapters in entry['editions'].items():
            from panelyra.services.download_identity import chapter_key
            editions[edition]=[{'id':n,'number':c['number'],'title':c['title'],'pages':len(c['images']),'verified':c['verified'],'healthy':c['healthy'],'status':c.get('status'),'error':c.get('error',''),'progress':records.get((edition,chapter_key(n) if c['number'] is not None else n)), 'url':self.reader_url(edition,key,n)} for n,c in chapters.items()]
        meta=entry['metadata']
        return {'id':key,'title':entry['title'],'metadata':{k:meta[k] for k in ('synopsis','description','author','genres','status','language') if meta.get(k)},'editions':editions,'cover':'/api/cover?'+urlencode({'series':key,'reader_token':self.reader_token}),'source':updates.mapping(self.app_dir,key,entry),'series_url':self.series_url(key)}

    def manifest(self,series,edition,number):
        data=self.public_series(series.removesuffix('-clean'));rows=data['editions'].get(edition,[])
        row=next((c for c in rows if c['id']==number),None)
        if row is None:
            from panelyra.services.download_identity import chapter_key
            try:row=next((c for c in rows if c['verified'] and chapter_key(c['id'])==chapter_key(number)),None)
            except ValueError:pass
        if row:number=row['id']
        chapter=self.chapter(edition,series,number)
        if not row or not chapter:raise ValueError('Chapter has no readable image files. Return to the series for repair options.')
        ordered=sorted([r for r in rows if r['verified'] and r['healthy']],key=lambda r:sort_key(r['id']))
        ids=[r['id'] for r in ordered];index=ids.index(number) if number in ids else -1
        return {'series':data['id'],'series_title':data['title'],'chapter':number,'title':row['title'],'edition':edition,'progress':row['progress'],'series_url':data['series_url'],'chapters':[{'id':r['id'],'url':r['url']} for r in ordered], 'previous':ids[index-1] if index>0 else None,'next':ids[index+1] if index>=0 and index+1<len(ids) else None,'images':[{'id':name,'width':size[0] or 800,'height':size[1] or 600,'valid':bool(size[0] and size[1]),'url':'/media/'+'/'.join(quote(p,safe='') for p in (edition,chapter.series,number,name))} for name,size in zip(chapter.images,chapter.dimensions)]}

    def reading_get(self,handler):
        req=urlsplit(handler.path);query={k:v[0] for k,v in parse_qs(req.query).items()};path=req.path
        if path.startswith('/assets/'):
            name=path.removeprefix('/assets/')
            if name not in {'reading.css','common.js','series.js','reader.js'}:self._send_text(handler,404,'Not found');return True
            self._send_file(handler,WEB/name);return True
        if path not in {'/series','/updates','/api/series','/api/manifest','/api/cover','/api/reading-preferences','/api/updates','/api/export'}:return False
        if not self.authorized(handler):self._send_text(handler,403,'Reader session expired. Reopen this title from Library.');return True
        try:
            series=query.get('series','');edition=query.get('edition','Original')
            if path in {'/series','/updates'}:
                if path=='/series':self.public_series(series)
                self._send_html(handler,self.page_html('series' if path=='/series' else 'updates',series));return True
            if path=='/api/series':result=self.public_series(series)
            elif path=='/api/manifest':result=self.manifest(series,edition,query.get('chapter',''))
            elif path=='/api/reading-preferences':result=read_json(self.app_dir/'data/reading_preferences.json').get(series,{})
            elif path=='/api/updates':
                result=updates.state(self.app_dir)
                if series:result={'jobs':[j for j in result['jobs'] if j['series'] in (None,series)],'results':{series:result['results'][series]} if series in result['results'] else {}}
                import fcntl
                lease=self.app_dir/'data/updates/discovery-worker.lock';alive=False
                if lease.exists():
                    with lease.open('r') as handle:
                        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(handle,fcntl.LOCK_UN)
                        except BlockingIOError:alive=True
                result['worker_running']=alive
            elif path=='/api/cover':
                entry=catalog(self.app_dir).get(series)
                if not entry:raise ValueError('Series not found.')
                cover=self.app_dir/'data/reader_covers'/(series+'.cover')
                if not cover.is_file():cover=next((p for rows in entry['editions'].values() for c in rows.values() for p in c['images'] if image_size(p)[0]),None)
                if not cover:raise ValueError('No cover available.')
                import io
                from PIL import Image
                with Image.open(cover) as im:
                    im.thumbnail((400,560));stream=io.BytesIO();im.convert('RGB').save(stream,'JPEG',quality=85)
                content=stream.getvalue();handler.send_response(200);handler.send_header('Content-Type','image/jpeg');handler.send_header('Content-Length',str(len(content)));handler.end_headers();handler.wfile.write(content);return True
            else:
                entry=catalog(self.app_dir).get(series,{});chapter=entry.get('editions',{}).get(edition,{}).get(query.get('chapter'))
                if not chapter:raise ValueError('Chapter not found.')
                if not chapter['healthy']:raise ValueError(chapter.get('error') or 'Chapter needs confirmation.')
                kind=query.get('kind','archive');files=[chapter['artifact']] if kind=='archive' and chapter.get('artifact') else chapter['images'][:1]
                if not files:raise ValueError('No saved archive is available for this chapter.')
                target=files[0]
                if target.is_symlink():raise ValueError('Linked files are not exported.')
                self._send_file(handler,target);return True
            self._send_json(handler,result)
        except (ValueError,OSError) as exc:self._send_json(handler,{'error':'Local files could not be accessed. Try again.' if isinstance(exc,OSError) else str(exc)})
        return True

    def reading_post(self,handler):
        path=urlsplit(handler.path).path
        if path not in {'/api/reading-preferences','/api/update-check','/api/source','/api/queue-selected'}:return False
        try:
            size=int(handler.headers.get('Content-Length',0))
            if not 0<size<=32000:raise ValueError('Invalid request size.')
            data=json.loads(handler.rfile.read(size))
            if not isinstance(data,dict):raise ValueError('Invalid request object.')
            series=data.get('series','')
            if not isinstance(series,str):raise ValueError('Invalid series identifier.')
            if data.get('reader_token')!=self.reader_token:self._send_text(handler,403,'Reader session expired. Reopen from Library.');return True
            if path=='/api/update-check':
                if data.get('scope')=='all':result={'job_id':updates.request(self.app_dir)}
                elif data.get('scope')=='series' and series:result={'job_id':updates.request(self.app_dir,series)}
                else:raise ValueError('Choose one series or explicitly check all series.')
            elif path=='/api/source':
                updates.save_mapping(self.app_dir,series,str(data.get('url','')).strip(),data.get('language','unknown'));result={'ok':True}
            elif path=='/api/reading-preferences':
                if series not in catalog(self.app_dir):raise ValueError('Series not found.')
                incoming=data.get('preferences',{})
                if not isinstance(incoming,dict):raise ValueError('Invalid reader preferences.')
                prefs={'mode':incoming.get('mode') if incoming.get('mode') in {'webtoon','single'} else 'webtoon','width':max(320,min(1600,int(incoming.get('width',880)))),'sizing':'original' if incoming.get('sizing')=='original' else 'fit','background':incoming.get('background') if incoming.get('background') in {'dark','black','paper'} else 'dark','seamless':bool(incoming.get('seamless',False))}
                def save(_):
                    p=self.app_dir/'data/reading_preferences.json';value=read_json(p);value[series]=prefs;atomic_write_json(p,value)
                updates.mutate(self.app_dir,save);result=prefs
            else:
                entry=catalog(self.app_dir).get(series)
                if not entry:raise ValueError('Series not found.')
                found=updates.state(self.app_dir)['results'].get(series,{})
                source=updates.mapping(self.app_dir,series,entry)
                if not source or source!=found.get('source'):raise ValueError('Check this source before choosing chapters.')
                if source.get('language')!='en':raise ValueError('The search downloader supports English. Confirm an English source, or use Capture for another language.')
                allowed={c['number']:c for c in found.get('chapters',[]) if c['availability']=='public_link'}
                selected=data.get('chapters',[])
                if not isinstance(selected,list) or not selected or any(n not in allowed for n in selected):raise ValueError('Choose available chapters from the latest check.')
                from panelyra.automation.download_queue import enqueue,ensure_worker
                job=enqueue(self.app_dir,source['url'],','.join(selected),entry['title'],catalog={'chapters':{n:allowed[n] for n in selected}});ensure_worker(self.app_dir);result={'job_id':job['id'],'downloads':self.dashboard_url+'?page=Downloads'}
            self._send_json(handler,result)
        except (ValueError,OSError,TypeError,KeyError) as exc:self._send_json(handler,{'error':'Local files could not be accessed. Try again.' if isinstance(exc,OSError) else str(exc)})
        return True
