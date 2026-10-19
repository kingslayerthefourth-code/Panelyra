"""Offline queue lifecycle, availability and duplicate protection checks."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from panelyra.automation import download_queue as q
from panelyra.automation.manga_backend import import_reviewed_archive
from panelyra.services.chapter_availability import parse_catalog, chapter_status
from panelyra.services.download_identity import expand_chapters, existing_chapters
URL='https://example.com/series/book'

def archive(path, color='blue'):
    data=io.BytesIO();Image.new('RGB',(12,12),color).save(data,format='PNG')
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('000.png',data.getvalue())

class Reliability(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def enqueue(self, chapters='1-3', **kw):
        kw.setdefault('catalog', {'chapters':{n:{'status':'public_link','url':URL+'/chapter-'+n} for n in expand_chapters(chapters)}})
        return q.enqueue(self.root,URL,chapters,'Book',**kw)
    def test_ranges(self):
        self.assertEqual(expand_chapters('1,1.0,2-3,4.5'),['1','2','3','4.5'])
        for bad in ('1.5-2.5','1-10001','3-1'):
            with self.assertRaises(ValueError):expand_chapters(bad)
    def test_retry_only_incomplete_and_completed_dedup(self):
        job=self.enqueue();calls=[]
        def run(root,job,n,stage,cancel):
            calls.append(n)
            if n!='2':archive(stage/(n+'.cbz'), 'blue' if n=='1' else 'red')
            return 0
        q.run_worker(self.root,run)
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'partial')
        q.retry_job(self.root,job['id'])
        def retry(root,job,n,stage,cancel):calls.append(n);archive(stage/(n+'.cbz'),'green');return 0
        q.run_worker(self.root,retry)
        self.assertEqual(calls,['1','2','3','2'])
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'completed')
        with self.assertRaises(ValueError):self.enqueue()
    def test_pause_resume_and_cancel(self):
        job=self.enqueue();calls=[]
        def run(root,job,n,stage,cancel):
            calls.append(n);archive(stage/(n+'.cbz'));q.set_paused(root,True);return 0
        q.run_worker(self.root,run)
        self.assertEqual(calls,['1'])
        self.assertTrue(q.read_queue(self.root)['paused'])
        q.cancel_job(self.root,job['id']);q.set_paused(self.root,False)
        q.run_worker(self.root,run);self.assertEqual(calls,['1'])
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'cancelled')
    def test_interrupted_worker_recovers_valid_archive(self):
        job=self.enqueue('1-2');stage=q.job_dir(self.root,job);archive(stage/'1.cbz')
        with q.transaction(self.root) as state:
            state['jobs'][0]['status']='running';state['jobs'][0]['chapters']['1']['status']='downloading'
        calls=[]
        def run(root,job,n,stage,cancel):calls.append(n);archive(stage/(n+'.cbz'));return 0
        q.run_worker(self.root,run);self.assertEqual(calls,['2'])
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'completed')
    def test_invalid_archive_not_success_and_preserved(self):
        job=self.enqueue('1');stage=q.job_dir(self.root,job);(stage/'1.cbz').write_bytes(b'bad')
        q.run_worker(self.root,lambda *args:0)
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'failed')
        self.assertEqual(len(list((stage/'incomplete').glob('*.cbz'))),1)
    def test_duplicate_queued_across_sessions(self):
        self.enqueue('1-2')
        job=self.enqueue('2-3');self.assertEqual(job['chapters']['2']['status'],'already_queued')
        with self.assertRaises(ValueError):self.enqueue('1')
    def test_single_worker_and_enqueue_while_running(self):
        self.enqueue('1');calls=[]
        def runner(root,job,n,stage,cancel):
            calls.append(n)
            q.run_worker(root,lambda *args: self.fail('Second worker obtained the lease'))
            if n=='1':self.enqueue('2')
            archive(stage/(n+'.cbz'));return 0
        q.run_worker(self.root,runner)
        self.assertEqual(calls,['1','2'])
        self.assertTrue(all(j['status']=='completed' for j in q.read_queue(self.root)['jobs']))
    def test_library_added_after_enqueue_is_skipped(self):
        self.enqueue('1');a=self.root/'1.cbz';archive(a)
        with patch('panelyra.automation.quiet_updates.updater_lock'):
            dest=import_reviewed_archive(a,URL,URL+'/chapter-1','book',output_dir=self.root/'manhwa',history_file=self.root/'history.json')
        # Archive-only libraries also count as saved.
        import shutil
        shutil.rmtree(dest/'images')
        q.run_worker(self.root,lambda *args:self.fail('Already saved chapter was downloaded'))
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['chapters']['1']['status'],'already_saved')
    def test_corrupt_state_preserved(self):
        q.base_dir(self.root).mkdir(parents=True);p=q.base_dir(self.root)/'queue.json';p.write_text('{bad')
        self.enqueue()  # Fresh SQLite queue leaves the old damaged JSON untouched.
        self.assertEqual(p.read_text(),'{bad')
    def test_import_duplicate_content_and_identity(self):
        a=self.root/'1.cbz';archive(a)
        with patch('panelyra.automation.quiet_updates.updater_lock'):
            import_reviewed_archive(a,URL,URL+'/chapter-1','book',output_dir=self.root/'manhwa',history_file=self.root/'history.json')
            self.assertIn('1',existing_chapters(self.root/'manhwa',URL,'Book'))
            with self.assertRaises(ValueError):self.enqueue('1')
            other = import_reviewed_archive(a,'https://example.com/other','https://example.com/other/chapter-1','other',output_dir=self.root/'manhwa',history_file=self.root/'history.json')
            self.assertTrue(other.is_dir())
    def test_availability_is_conservative(self):
        html='<div id="chapters"><a href="/series/book/chapter-1">Chapter 1</a><li data-num="2" class="locked">Chapter 2 premium</li></div>'
        result=parse_catalog(html,URL,'Test')
        self.assertEqual(chapter_status(result,'1')['status'],'public_link')
        self.assertEqual(chapter_status(result,'2')['status'],'locked')
        self.assertEqual(chapter_status(result,'3')['status'],'unknown')
        result=parse_catalog(html,URL,'Test',full_list=True)
        self.assertEqual(chapter_status(result,'3')['status'],'missing')
        job=self.enqueue('1-3',catalog=result)
        self.assertEqual([c['status'] for c in job['chapters'].values()],['pending','locked','missing'])
    def test_weeb_uuid_links(self):
        result=parse_catalog('<a href="/chapters/ABC123">Chapter 12</a>','https://weebcentral.com/series/XYZ/Book','WeebCentral',full_list=True)
        self.assertEqual(result['chapters']['12']['status'],'public_link')
    def test_cancellation_interrupt_keeps_resume_state(self):
        job=self.enqueue('1')
        def runner(root,job,n,stage,cancel):
            q.cancel_job(root,job['id']);self.assertTrue(cancel());raise InterruptedError('Stopped')
        q.run_worker(self.root,runner)
        self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'cancelled')
        q.retry_job(self.root,job['id']);self.assertEqual(q.read_queue(self.root)['jobs'][0]['status'],'queued')

    def test_completed_archive_is_published_even_when_interruption_arrives(self):
        job=self.enqueue('1')
        def runner(root,job,n,stage,cancel):
            archive(stage/(n+'.cbz'))
            q.cancel_job(root,job['id'])
            raise InterruptedError('Cancelled at final byte')
        q.run_worker(self.root,runner)
        state=q.read_queue(self.root);live=state['jobs'][0]
        self.assertEqual(live['status'],'cancelled')
        self.assertEqual(live['chapters']['1']['status'],'downloaded')
        self.assertIn('1',existing_chapters(self.root/'manhwa',URL,'Book'))
        self.assertFalse(any(state['source_failures'].values()))

    def test_cancelled_chapter_never_spawns_backend(self):
        job=self.enqueue('1')
        with patch.object(q.subprocess,'Popen',side_effect=AssertionError('Spawned after cancellation')):
            with self.assertRaises(InterruptedError):q.execute_chapter(self.root,job,'1',q.job_dir(self.root,job),lambda:True)

    def test_interrupted_unconfirmed_archive_is_retained_and_explained(self):
        job=self.enqueue('1',catalog={})
        def runner(root,job,n,stage,cancel):
            archive(stage/(n+'.cbz'));raise InterruptedError('Paused')
        q.run_worker(self.root,runner)
        live=q.read_queue(self.root)['jobs'][0]
        self.assertEqual(live['status'],'interrupted')
        self.assertEqual(live['chapters']['1']['status'],'pending')
        self.assertIn('Needs confirmation',live['chapters']['1']['error'])
        self.assertTrue((q.job_dir(self.root,job)/'1.cbz').is_file())

if __name__=='__main__':unittest.main()
