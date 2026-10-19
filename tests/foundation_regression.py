"""Desired behaviour for the foundation audit defects; no live downloads/devices."""
import io
import os
import json
import multiprocessing
import subprocess
import hashlib
import hmac
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from panelyra.services.library_store import LibraryStore, validate_archive, canonical_source, transaction, db_path
from panelyra.services.download_identity import existing_chapters, page_fingerprint, expand_chapters
from panelyra.services.reader_progress import ReaderProgressStore
from panelyra.services.reading_catalog import catalog, sort_key
from panelyra.services.local_reader import LocalReaderService
from panelyra.automation import download_queue as queue
from sync_library import OriginalLibrary

def png(color='white'):
    stream=io.BytesIO(); Image.new('RGB',(24,36),color).save(stream,'PNG');return stream.getvalue()

def chapter(root, folder='A book (English)', number='01', source='https://one.example/series/a', raw=False, archive=False):
    directory=Path(root)/'manhwa'/folder/number;directory.mkdir(parents=True)
    if archive:
        with zipfile.ZipFile(directory/'chapter-pages.cbz','w') as z:z.writestr('000.png',png())
    else:
        (directory/'images').mkdir();(directory/'images'/'page1.png').write_bytes(png())
    if not raw:
        (directory/'metadata.json').write_text(json.dumps({'series_title':'Same Title','series_url':source,'language':'en','image_count':1,'saved_image_count':1,'identity_verified':True}))
    return directory

def write_progress(path, number):
    ReaderProgressStore(Path(path)).update({'series':'a','chapter':number,'total_panels':4,'panel':2})

class FoundationTests(unittest.TestCase):
    def test_capture_publishes_only_after_pages_are_complete(self):
        import manhwa_ocr_downloader as capture
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);output=root/'manhwa'/'capture-book';output.mkdir(parents=True)
            class Driver:current_url='https://one.example/book/chapter-1'
            def save(driver,urls,folder,*args,**kwargs):
                folder.mkdir();(folder/'001.png').write_bytes(png());return 1
            options=dict(chapter_index=1,output_dir=output,series_title='Capture Book',series_slug='capture-book',
                container_selector='',image_selector='',confidence=.5,scroll_min_delay=0,scroll_max_delay=0,image_fetch_timeout=1,
                save_raw_images=True,create_archive=True,save_ocr=False,reader_layout='auto',next_chapter_selector='',next_page_selector='',page_counter_selector='',chapter_list_selector='')
            with patch.object(capture,'chapter_title',return_value='Chapter 1'),patch.object(capture,'wait_for_chapter_media',return_value=True),patch.object(capture,'collect_chapter_image_urls',return_value=['https://one.example/1.png']),patch.object(capture,'save_images',side_effect=save):
                result=capture.ocr_current_chapter(Driver(),**options)
            self.assertEqual(result.status,'ok')
            with patch.object(capture,'chapter_title',return_value='Chapter 1'),patch.object(capture,'save_images',side_effect=AssertionError('Committed chapter must not download again')):
                recovered=capture.ocr_current_chapter(Driver(),**options)
            self.assertEqual(recovered.status,'ok')
            self.assertEqual(recovered.image_count,1)
            self.assertEqual(len(list(output.glob('.receiving-*'))),0)
            self.assertTrue(next(iter(catalog(root)['capture-book']['editions']['Original'].values()))['healthy'])

    def test_source_breaker_stops_after_three_and_other_source_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            queue.enqueue(root,'https://one.example/book','1-9','One')
            queue.enqueue(root,'https://two.example/book','1','Two')
            calls=[]
            def failed(root,job,n,stage,cancel):calls.append((job['title'],n));return 1
            queue.run_worker(root,failed)
            self.assertEqual([c for c in calls if c[0]=='One'],[('One','1'),('One','2'),('One','3')])
            self.assertEqual([c for c in calls if c[0]=='Two'],[('Two','1')])
            self.assertEqual(queue.read_queue(root)['jobs'][0]['status'],'paused')

    def test_paired_discovery_requires_nonce_pin_and_authenticator(self):
        from panelyra.services.tablet_discovery import response_valid
        device={'pin':'a'*64,'token':'b'*64};nonce='c'*64
        body={'nonce':nonce,'pin':device['pin'],'port':8590}
        body['proof']=hmac.new(device['token'].encode(),f"discover|{nonce}|{device['pin']}|8590".encode(),hashlib.sha256).hexdigest()
        self.assertTrue(response_valid(body,nonce,device))
        self.assertFalse(response_valid(body,'d'*64,device))
        self.assertFalse(response_valid(dict(body,pin='e'*64),nonce,device))
        self.assertFalse(response_valid(dict(body,proof='0'*64),nonce,device))

    def test_publishing_zip_order_matches_numerical_story_order(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'1';directory.mkdir()
            with zipfile.ZipFile(directory/'chapter-pages.cbz','w') as z:
                for n,color in [(10,'blue'),(2,'green'),(1,'red')]:z.writestr(f'{n}.png',png(color))
            helper=Path(os.environ.get('PANELYRA_PUBLISH_HELPER', '/nonexistent/panelyra-publish-helper.py'))
            if not helper.exists():
                self.skipTest('Optional external publishing helper is not part of the Panelyra source distribution')
            subprocess.run([sys.executable,str(helper),str(directory)],check=True,capture_output=True)
            with zipfile.ZipFile(directory/'chapter-1.zip') as z:
                self.assertEqual([z.read(n) for n in z.namelist()],[png('red'),png('green'),png('blue')])

    def test_source_identity_and_order(self):
        self.assertNotEqual(canonical_source('https://comic.naver.com/webtoon/detail?titleId=845837&no=23'), canonical_source('https://comic.naver.com/webtoon/detail?titleId=851610&no=1'))
        self.assertEqual(canonical_source('https://comic.naver.com/webtoon/detail?titleId=845837&no=23'),canonical_source('https://comic.naver.com/webtoon/list?titleId=845837'))
        self.assertEqual(sorted(['page10.png','page1.png','page2.png'],key=sort_key),['page1.png','page2.png','page10.png'])
        self.assertNotEqual(page_fingerprint([('1',b'a'),('2',b'b')]),page_fingerprint([('1',b'b'),('2',b'a')]))
        self.assertEqual(len(expand_chapters('1-10000')),10000)
        self.assertEqual(sorted(['1.10','1.2','1'],key=sort_key),['1','1.10','1.2'])
        from manhwa_ocr_downloader import resolve_series_output_dir
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)
            a=resolve_series_output_dir(output,'Same Title','same-title',source_url='https://one.example/book/chapter-1')
            b=resolve_series_output_dir(output,'Same Title','same-title',source_url='https://two.example/book/chapter-1')
            self.assertNotEqual(a,b)

    def test_no_title_merge_or_incomplete_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);chapter(root);chapter(root,'unfinished','2',raw=True)
            self.assertFalse(existing_chapters(root/'manhwa','https://two.example/series/a','Same Title'))
            entries=catalog(root)
            self.assertTrue(entries['A book (English)']['editions']['Original']['01']['healthy'])
            self.assertFalse(entries['unfinished']['editions']['Original']['2']['healthy'])
            exported=OriginalLibrary(root).catalog()
            self.assertEqual(len(exported['chapters']),1)
            self.assertEqual(len(exported['errors']),1)
            self.assertRegex(exported['chapters'][0]['series_key'],r'^[a-f0-9]{32}$')

    def test_cbz_only_cache_reuse_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);chapter(root,archive=True)
            c=catalog(root)['A book (English)']['editions']['Original']['01'];self.assertTrue(c['healthy'])
            page=c['images'][0];modified=page.stat().st_mtime_ns
            again=catalog(root)['A book (English)']['editions']['Original']['01']
            self.assertEqual(again['id'],c['id']);self.assertEqual(page.stat().st_mtime_ns,modified)
            service=LocalReaderService(root)
            self.assertTrue(service.chapter('Original','A book (English)','01'))
            self.assertTrue(service.chapter('Original','A book (English)','1'))

    def test_corrupt_pixels_and_duplicate_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'bad.cbz';stream=io.BytesIO();Image.new('RGB',(200,200),'red').save(stream,'JPEG');truncated=stream.getvalue()[:-80]
            with zipfile.ZipFile(file,'w') as z:z.writestr('0.jpg',truncated)
            with self.assertRaises(Exception):validate_archive(file)
            with zipfile.ZipFile(file,'w') as z:z.writestr('../0.png',png())
            with self.assertRaises(ValueError):validate_archive(file)
            with zipfile.ZipFile(file,'w') as z:z.writestr('0.png',png());z.writestr('0.PNG',png())
            with self.assertRaises(ValueError):validate_archive(file)

    def test_fresh_progress_preserves_legacy_and_parallel_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'reader_progress.json';path.write_text('{damaged legacy')
            workers=[multiprocessing.Process(target=write_progress,args=(str(path),str(n))) for n in range(8)]
            for p in workers:p.start()
            for p in workers:p.join(30);self.assertEqual(p.exitcode,0)
            store=ReaderProgressStore(path);self.assertEqual(len(store.all()),8)
            self.assertIsNotNone(store.get('a','01','Original'))
            self.assertEqual(path.read_text(),'{damaged legacy')
            store.mark('a','1','Original',True);store.update({'series':'a','chapter':'1','panel':1})
            self.assertTrue(store.get('a','1','Original').completed)

    def test_reader_auth_cookie_host_origin_and_cached_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);chapter(root,archive=True)
            service=LocalReaderService(root).start()
            try:
                path='/reader?edition=Original&series=A%20book%20%28English%29&chapter=01'
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(service.base_url+path)
                self.assertEqual(caught.exception.code,403)
                response=urllib.request.urlopen(service.base_url+path+'&reader_token='+service.reader_token)
                cookie=response.headers['Set-Cookie'].split(';')[0];response.read()
                manifest=json.load(urllib.request.urlopen(urllib.request.Request(service.base_url+'/api/manifest?series=A%20book%20%28English%29&edition=Original&chapter=01',headers={'Cookie':cookie})))
                media=service.base_url+manifest['images'][0]['url']
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(media)
                self.assertEqual(urllib.request.urlopen(urllib.request.Request(media,headers={'Cookie':cookie})).read(),png())
                for headers in ({'Host':'evil.example'}, {'Origin':'https://evil.example'}):
                    with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(urllib.request.Request(service.base_url+path+'&reader_token='+service.reader_token,headers=headers))
            finally:service._server.shutdown();service._server.server_close()

    def test_recover_after_publish_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);staged=chapter(root,'staging','1');destination=root/'manhwa'/'book'/'1';destination.parent.mkdir()
            def fault(stage):
                if stage=='renamed':raise RuntimeError('simulated power failure')
            store=LibraryStore(root)
            with self.assertRaises(RuntimeError):store.publish(staged,destination,fault)
            store.recover();self.assertTrue(catalog(root)['book']['editions']['Original']['1']['healthy'])

    def test_durable_queue_title_separation_pause_cancel_and_10000_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first=queue.enqueue(root,'https://one.example/series/a','1-10000','Same Title')
            second=queue.enqueue(root,'https://two.example/series/a','1','Same Title')
            self.assertNotEqual(first['id'],second['id'])
            queue.set_paused(root,True);queue.cancel_job(root,second['id'])
            state=queue.read_queue(root);self.assertTrue(state['paused']);self.assertEqual(len(state['jobs'][0]['chapters']),10000)
            self.assertTrue(state['jobs'][1]['cancel_requested'])

    def test_1000_chapter_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for n in range(1,1001):chapter(root,number=str(n))
            started=time.monotonic();first=catalog(root);cold=time.monotonic()-started
            started=time.monotonic();warm=catalog(root);elapsed=time.monotonic()-started
            self.assertEqual(len(first['A book (English)']['editions']['Original']),1000)
            self.assertEqual(len(warm['A book (English)']['editions']['Original']),1000)
            print(f'1000 tiny synthetic chapters: first index {cold:.3f}s, reopen {elapsed:.3f}s')

if __name__=='__main__':unittest.main(verbosity=2)
