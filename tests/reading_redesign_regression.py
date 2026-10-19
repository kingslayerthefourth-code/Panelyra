"""Offline integration tests for the new reading flow and discovery-only updater."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from panelyra.services.reading_catalog import catalog,audit
from panelyra.services import reading_updates as u
from panelyra.services.local_reader import LocalReaderService
from panelyra.services.reader_progress import ReaderProgressStore

class ReadingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.service=None
  self.make('book','1');self.make('book','2.5');self.make('book','10');self.make('other','1')
 def tearDown(self):
  if self.service:self.service._server.shutdown();self.service._server.server_close()
  self.tmp.cleanup()
 def make(self,series,chapter,edition='Original',broken=False):
  folder=self.root/('manhwa' if edition=='Original' else 'manhwa_clean')/series/chapter; (folder/'images').mkdir(parents=True)
  p=folder/'images/001.png'
  if broken:p.write_bytes(b'bad')
  else:Image.new('RGB',(60,500),'navy').save(p)
  (folder/'metadata.json').write_text(json.dumps({'series_title':series.title(),'series_url':'https://example.com/series/'+series,'image_count':1,'language':'en'}))
  return folder
 def start(self):self.service=LocalReaderService(self.root).start();return self.service
 def get(self,path):return json.loads(urlopen(self.service.base_url+path,timeout=5).read())
 def test_catalog_numeric_editions_and_suspicious_ids(self):
  self.make('book','2103450');self.make('book-clean','1','Clean');self.make('book','3',broken=True)
  c=catalog(self.root)['book'];self.assertIn('Clean',c['editions']);self.assertFalse(c['editions']['Original']['2103450']['verified']);self.assertFalse(c['editions']['Original']['3']['healthy']);self.assertEqual(len(audit(self.root)),2)
  s=self.start();self.assertEqual(s._chapter_options('Original','book'),['1','2.5','10'])
 def test_public_model_hides_paths_and_preserves_labels(self):
  s=self.start();result=s.public_series('book');encoded=json.dumps(result);self.assertNotIn(str(self.root),encoded);self.assertEqual(result['title'],'Book');self.assertTrue(result['cover'].startswith('/api/cover'))
  m=s.manifest('book','Original','2.5');self.assertEqual((m['previous'],m['next']),('1','10'));self.assertEqual(m['images'][0]['height'],500)
 def test_api_authentication_and_assets(self):
  s=self.start()
  with self.assertRaises(HTTPError) as error:urlopen(s.base_url+'/api/series?series=book')
  self.assertEqual(error.exception.code,403)
  result=self.get('/api/series?series=book&reader_token='+s.reader_token);self.assertEqual(result['id'],'book')
  with self.assertRaises(HTTPError):urlopen(s.base_url+'/assets/../../metadata.json')
  page=urlopen(s.series_url('book')).read().decode();self.assertIn('/assets/common.js',page);self.assertNotIn('LYRA',page)
 def test_progress_image_anchor_and_legacy_records(self):
  store=ReaderProgressStore(self.root/'progress.json')
  old=store.update({'series':'book','chapter':'1','panel':1,'mode':'comfort','offset':500})
  self.assertEqual(old.mode,'comfort')
  p=store.update({'series':'book','chapter':'1','image_id':'001.png','image_fraction':.43,'mode':'webtoon'})
  self.assertEqual((p.image_id,p.image_fraction),('001.png',.43));self.assertEqual(store.get('book','1','Original').mode,'webtoon')
 def test_opaque_ids_use_visible_labels_only(self):
  r=u.parse_list('<div id="chapters"><a href="/series/book/chapter-2103450">Chapter 79</a><a href="/series/book/chapter-2103451">Read</a></div>','https://example.com/series/book')
  self.assertEqual(list(r['chapters']),['79']);self.assertIn('2103450',r['chapters']['79']['source_id']);self.assertFalse(r['complete'])
 def test_sibling_chapter_urls_use_visible_labels(self):
  result=u.parse_list('<a href="/manga/book-chapter-987654/">Chapter 38</a>', 'https://example.com/manga/book')
  self.assertEqual(list(result['chapters']),['38'])
  self.assertEqual(result['status'],'partial')
 def test_challenge_partial_and_locked(self):
  self.assertEqual(u.parse_list('Verify you are human','https://example.com/book')['status'],'blocked')
  r=u.parse_list('<div id="chapters"><li class="locked"><a href="/book/chapter-1">Chapter 1 premium</a></li></div>','https://example.com/book');self.assertEqual(r['chapters']['1']['status'],'locked')
 def test_scoped_check_no_image_downloads(self):
  calls=[]
  def fetch(url):calls.append(url);return '<a href="/series/book/chapter-300001">Chapter 11</a>'
  with patch('panelyra.services.reading_updates.subprocess.Popen'):
   u.request(self.root,'book')
  with patch('panelyra.automation.quiet_updates.quiet_download_chapter',side_effect=AssertionError('Downloaded!')),patch('panelyra.automation.download_queue.enqueue',side_effect=AssertionError('Queued!')):
   u.run(self.root,fetch)
  self.assertEqual(calls,['https://example.com/series/book']);self.assertEqual(u.state(self.root)['results']['book']['new_count'],1);self.assertNotIn('other',u.state(self.root)['results'])
 def test_saved_and_queued_comparison(self):
  entry=catalog(self.root)['book']
  result=u.check(self.root,'book',entry,lambda url:'<a href="/series/book/chapter-987654">Chapter 1</a><a href="/series/book/chapter-999999">Chapter 12</a>')
  self.assertEqual([c['availability'] for c in result['chapters']],['saved','public_link'])
 def test_missing_source_and_network_failure(self):
  entry=catalog(self.root)['book'];entry['editions']['Original']['1']['metadata']['series_url']='https://different.com/book'
  self.assertEqual(u.check(self.root,'book',entry,lambda _:self.fail('should not fetch'))['status'],'needs_source')
  entry=catalog(self.root)['book']
  def fail(_):raise OSError('Offline')
  self.assertEqual(u.check(self.root,'book',entry,fail)['status'],'failed')
 def test_repeated_request_and_recovery(self):
  with patch('panelyra.services.reading_updates.subprocess.Popen'):
   a=u.request(self.root,'book');b=u.request(self.root,'book')
  self.assertEqual(a,b)
  u.mutate(self.root,lambda state:state['jobs'][0].update(state='running',pid=999999))
  u.run(self.root,lambda url:'<a href="/series/book/chapter-44">Chapter 44</a>')
  self.assertEqual(u.state(self.root)['jobs'][0]['state'],'finished')
 def test_schedule_is_discovery_only(self):
  with patch.object(u,'run',side_effect=AssertionError('Timer must not own a worker')),patch('panelyra.automation.quiet_updates.quiet_download_chapter',side_effect=AssertionError('Downloaded!')):
   u.scheduled(self.root)
   self.assertEqual(u.state(self.root)['jobs'],[])
   with patch('panelyra.services.worker_runtime.engine_pid',return_value=123):u.scheduled(self.root)
  self.assertEqual(len(u.state(self.root)['jobs']),1);self.assertEqual(len(list((self.root/'manhwa').glob('*/*/images/*.png'))),4)
 def test_selected_downloads_persist_ids_and_reject_duplicates(self):
  s=self.start()
  result=u.check(self.root,'book',catalog(self.root)['book'],lambda _: '<a href="/series/book/chapter-987654">Chapter 12</a>')
  u.mutate(self.root,lambda value:value['results'].update(book=result))
  payload=json.dumps({'reader_token':s.reader_token,'series':'book','chapters':['12']}).encode()
  from panelyra.automation.download_queue import read_queue
  with patch('panelyra.automation.download_queue.ensure_worker') as worker:
   request=Request(s.base_url+'/api/queue-selected',data=payload,headers={'Content-Type':'application/json'})
   response=json.loads(urlopen(request).read());self.assertIn('job_id',response)
   duplicate=json.loads(urlopen(request).read());self.assertIn('error',duplicate)
   worker.assert_called_once()
  jobs=read_queue(self.root)['jobs'];self.assertEqual(len(jobs),1)
  self.assertEqual(list(jobs[0]['chapters']),['12'])
  self.assertTrue(jobs[0]['chapters']['12']['url'].endswith('chapter-987654'))
 def test_invalid_request_shapes(self):
  s=self.start()
  request=Request(s.base_url+'/api/source',data=b'[]',headers={'Content-Type':'application/json'})
  self.assertIn('error',json.loads(urlopen(request).read()))
 def test_legacy_library_cli_redirects_to_discovery(self):
  from panelyra.automation.series_updates import main
  with patch.object(u,'scheduled') as schedule:
   main(['--library','--quiet','--once','--library-root',str(self.root/'manhwa')])
  schedule.assert_called_once_with(self.root)

if __name__=='__main__':unittest.main()
