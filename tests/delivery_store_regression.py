"""Durability/migration/concurrency fixtures; no real tablet or source requests."""
import json
import multiprocessing
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_tablet_delivery as fixtures
from tablet_delivery import DeliveryStore
from panelyra.services.library_store import transaction,connect,db_path


def ack(root,identity,payload):DeliveryStore(Path(root)).update(identity,'ack',payload,summary=True)


class StoreTests(unittest.TestCase):
    setUp=fixtures.DeliveryTest.setUp
    tearDown=fixtures.DeliveryTest.tearDown

    def make(self):return self.store.create([{'series_key':'book-a','chapter':str(n)} for n in range(1,6)])

    def legacy(self,value):
        path=self.store.root/(value['transfer_id']+'.json');path.write_text(json.dumps(value))
        with transaction(db_path(self.root)) as db:
            db.execute('DELETE FROM transfers');db.execute("DELETE FROM settings WHERE key='delivery_json_imported'")
        return path

    def test_import_existing_partial_state_once_and_preserve_json(self):
        value=self.store.get(self.make()['transfer_id']);first=value['chapters'][0]
        value['acknowledged']=[f"{first['series_key']}/{first['chapter']}"]
        value['status']='paused';value['received_bytes']=first['size'];path=self.legacy(value);original=path.read_bytes()
        store=DeliveryStore(self.root);recovered=store.get(value['transfer_id'])
        self.assertEqual(recovered['status'],'paused');self.assertEqual(recovered['acknowledged'],value['acknowledged'])
        self.assertEqual(recovered['received_bytes'],first['size']);self.assertEqual(path.read_bytes(),original)
        store.update(value['transfer_id'],'resume',{})
        self.assertEqual(DeliveryStore(self.root).get(value['transfer_id'])['status'],'queued')
        self.assertEqual(path.read_bytes(),original)

    def test_bad_legacy_record_reported_without_hiding_valid_delivery(self):
        value=self.store.get(self.make()['transfer_id']);path=self.legacy(value)
        damaged=self.store.root/(uuid.uuid4().hex+'.json');damaged.write_text('{damaged')
        store=DeliveryStore(self.root)
        self.assertEqual(len(store.list()),1);self.assertEqual(store.issues()[0]['origin'],damaged.name)
        self.assertEqual(damaged.read_text(),'{damaged');self.assertTrue(path.exists())

    def test_unsafe_path_and_false_ready_are_not_imported(self):
        value=self.store.get(self.make()['transfer_id']);value['status']='ready';self.legacy(value)
        store=DeliveryStore(self.root);self.assertEqual(store.list(),[])
        self.assertIn('acknowledgements',store.issues()[0]['error'])
        value['status']='paused';key=next(iter(value['files']));value['files'][key]='/private/unrelated.cbz';self.legacy(value)
        store=DeliveryStore(self.root);self.assertEqual(store.list(),[])
        self.assertIn('outside',store.issues()[0]['error'])

    def test_missing_old_file_is_retained_as_explicit_failure_not_removed(self):
        value=self.store.get(self.make()['transfer_id']);first=value['chapters'][0]
        path=Path(value['files'][f"{first['series_key']}/{first['chapter']}"]);path.unlink()
        # The manifest must survive even if the Mac file is no longer available.
        self.legacy(value);store=DeliveryStore(self.root)
        self.assertEqual(len(store.list()),1)
        with self.assertRaises((OSError,ValueError)):store.file(value['transfer_id'],first['series_key'],first['chapter'])

    def test_parallel_acknowledgements_and_late_progress_do_not_lose_ready(self):
        value=self.make();workers=[]
        for chapter in value['chapters']:
            payload={k:chapter[k] for k in ('series_key','chapter','sha256')}
            workers.append(multiprocessing.Process(target=ack,args=(str(self.root),value['transfer_id'],payload)))
        for worker in workers:worker.start()
        for worker in workers:worker.join(20);self.assertEqual(worker.exitcode,0)
        self.store.update(value['transfer_id'],'progress',{'status':'receiving','received_bytes':0})
        final=self.store.get(value['transfer_id']);self.assertEqual(final['status'],'ready')
        self.assertEqual(len(final['acknowledged']),5);self.assertEqual(final['received_bytes'],final['total_bytes'])

    def test_cancel_and_pause_cannot_be_resurrected_by_late_progress(self):
        for action,expected in [('pause','paused'),('cancel','cancelled')]:
            value=self.make();self.store.update(value['transfer_id'],action,{})
            self.store.update(value['transfer_id'],'progress',{'status':'receiving','received_bytes':999})
            self.assertEqual(DeliveryStore(self.root).status(value['transfer_id']),expected)

    def test_transaction_failure_does_not_publish_partial_manifest(self):
        original=self.store._insert
        def fail(db,value):original(db,value);raise RuntimeError('simulated interruption')
        with patch.object(self.store,'_insert',side_effect=fail):
            with self.assertRaises(RuntimeError):self.make()
        self.assertEqual(self.store.list(),[])
        with connect(db_path(self.root)) as db:self.assertEqual(db.execute('SELECT count(*) FROM transfer_chapters').fetchone()[0],0)

    def test_manual_usb_uses_shared_artifacts_not_canonical_ids_as_folders(self):
        import usb_sync
        calls=[]
        def adb(*args,**kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,'{}' if args[:2]==('shell','cat') else '','')
        names={'APP_DIR':self.root,'APPROVAL':self.root/'data/tablet_companion/approved-usb.json',
               'USB_STATUS':self.root/'data/tablet_companion/usb-status.json'}
        with patch.multiple(usb_sync,**names),patch.object(usb_sync,'approved_serial',return_value='TEST-TABLET'),\
             patch.object(usb_sync,'connected_devices',return_value=[{'serial':'TEST-TABLET','status':'device'}]),patch.object(usb_sync,'_adb',side_effect=adb):
            result=usb_sync._sync_once_unlocked()
        self.assertEqual(result['copied'],10)
        cbzs=[Path(call[1]) for call in calls if call[0]=='push' and call[1].endswith('.cbz')]
        self.assertEqual(len(cbzs),10);self.assertTrue(all(file.is_file() for file in cbzs))

    def test_alias_and_decimal_duplicates_make_one_entry(self):
        chapter=self.library.catalog()['chapters'][0]
        selection=[{'series_key':chapter['series_key'],'chapter':chapter['chapter']},
                   {'series_key':chapter['library_key'],'chapter':'01'}]
        value=self.store.create(selection);self.assertEqual(len(value['chapters']),1)

    def test_large_manifest_progress_does_not_rebuild_entire_manifest(self):
        source=self.library.catalog()['chapters'][0];file=self.library.chapter_file(source['series_key'],source['chapter']);cover=self.library.cover_file(source['series_key'])
        chapters=[dict(source,chapter=str(n)) for n in range(1,10001)]
        library=self.library
        class Library:
            def catalog(self):return {'chapters':chapters}
            def chapter_file(self,*args):return file
            def cover_file(self,*args):return cover
            def verified_digest(self,*args):return library.verified_digest(*args)
        store=DeliveryStore(self.root,Library());started=time.monotonic()
        value=store.create([{'series_key':source['series_key'],'chapter':str(n)} for n in range(1,10001)])
        self.assertEqual(len(value['chapters']),10000)
        first=value['chapters'][0];started=time.monotonic()
        with patch.object(store,'_get',side_effect=AssertionError('Full manifest reread during progress')):
            result=store.update(value['transfer_id'],'progress',{'status':'receiving','received_bytes':50},summary=True)
            self.assertEqual(store.status(value['transfer_id']),'receiving')
            store.update(value['transfer_id'],'ack',{k:first[k] for k in ('series_key','chapter','sha256')},summary=True)
            self.assertEqual(store.file(value['transfer_id'],first['series_key'],first['chapter'])[0],file)
        self.assertNotIn('chapters',result)
        print(f'10000-chapter fixture: progress and ack {time.monotonic()-started:.4f}s; not Wi-Fi throughput')


if __name__=='__main__':unittest.main(verbosity=2)
