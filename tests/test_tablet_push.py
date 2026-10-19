import hashlib
import json
import os
import socket
import threading
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from tablet_delivery import DeliveryStore
from tablet_push import endpoint, save_device, load_device, PushError, deliver, PushWorker, TransferControl


class PushTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        folder = self.root / 'manhwa/book/01'; folder.mkdir(parents=True)
        page = self.root / 'page.png'; Image.new('RGB', (30, 30), '#123456').save(page)
        (folder / 'metadata.json').write_text(json.dumps({'series_title': 'Book', 'url': 'https://book.test/chapter-1','image_count':1,'language':'en'}))
        with zipfile.ZipFile(folder / 'chapter-pages.cbz', 'w') as archive: archive.write(page, '01.png')
        self.store = DeliveryStore(self.root)
        self.device = {'address': '192.168.1.20:8590', 'token': 'a'*64, 'pin': 'b'*64, 'device': 'Test Tablet'}
        self.calls = []
    def tearDown(self): self.temp.cleanup()
    def create(self): return self.store.create([{'series_key': 'book', 'chapter': '01'}], transport='push', target_pin=self.device['pin'])
    def client(self):
        owner = self
        class Fake:
            def __init__(self, *args): pass
            def request(self, *args): return {'status': 'ready'}, owner.device['pin']
            def send_file(self, file, meta, progress, checkpoint):
                checkpoint(); owner.calls.append(meta['kind'])
                owner.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), meta['sha256'])
                progress(meta['size'])
        return Fake
    def test_local_endpoints_and_private_pairing_storage(self):
        for bad in ('http://192.168.1.1', 'https://user:secret@192.168.1.1', '8.8.8.8', '192.168.1.1/path', '192.168.1.1?x', '0.0.0.0'):
            with self.assertRaises(ValueError): endpoint(bad)
        self.assertEqual(endpoint('192.168.1.20'), ('192.168.1.20', 8590))
        save_device(self.root, self.device)
        self.assertEqual(load_device(self.root), self.device)
        self.assertEqual(os.stat(self.root / 'data/tablet_companion/push-device.json').st_mode & 0o777, 0o600)
    def test_chapter_and_cover_acknowledged_only_after_install(self):
        item = self.create()
        self.assertEqual(self.store.list(pending=True, transport='pull'), [])
        self.assertEqual(len(self.store.list(pending=True, transport='push')), 1)
        with patch('tablet_push.PushClient', self.client()): deliver(self.root, item['transfer_id'], self.device)
        self.assertEqual(self.calls, ['chapter', 'cover'])
        final = self.store.get(item['transfer_id'])
        self.assertEqual(final['status'], 'ready')
        self.assertEqual(final['acknowledged'], [item['chapters'][0]['series_key']+'/1'])
    def test_paused_cancelled_or_wrong_tablet_never_send(self):
        for action in ('pause', 'cancel', 'wrong-tablet'):
            item = self.create()
            if action != 'wrong-tablet': self.store.update(item['transfer_id'], action, {})
            device = dict(self.device, pin='c'*64) if action == 'wrong-tablet' else self.device
            with patch('tablet_push.PushClient', self.client()): deliver(self.root, item['transfer_id'], device)
        self.assertEqual(self.calls, [])
    def test_interrupted_transfer_remains_retryable_and_unacknowledged(self):
        item = self.create()
        class Offline(self.client()):
            def send_file(self, *args): raise OSError('offline')
        with patch('tablet_push.PushClient', Offline): deliver(self.root, item['transfer_id'], self.device)
        self.assertEqual(self.store.get(item['transfer_id'])['status'], 'failed')
        self.assertEqual(self.store.get(item['transfer_id'])['acknowledged'], [])
        self.store.update(item['transfer_id'], 'resume', {})
        with patch('tablet_push.PushClient', self.client()): deliver(self.root, item['transfer_id'], self.device)
        self.assertEqual(self.store.get(item['transfer_id'])['status'], 'ready')
    def test_worker_does_not_retry_failures_or_touch_legacy_queue(self):
        save_device(self.root, self.device)
        failed = self.create(); self.store.update(failed['transfer_id'], 'progress', {'status': 'failed'})
        self.store.create([{'series_key': 'book', 'chapter': '1'}])
        queued = self.create()
        worker=PushWorker(self.root)
        with patch('tablet_push.deliver') as send, patch('tablet_push.PushClient',self.client()): worker.run()
        send.assert_called_once_with(self.root, queued['transfer_id'], self.device, worker.stopped)

    def test_engine_stop_prevents_send_and_leaves_transfer_retryable(self):
        item=self.create();worker=PushWorker(self.root);worker.stop()
        with patch('tablet_push.PushClient', self.client()):
            deliver(self.root,item['transfer_id'],self.device,worker.stopped)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.get(item['transfer_id'])['acknowledged'],[])

    def test_stop_interrupts_blocked_socket(self):
        reader,writer=socket.socketpair();control=TransferControl();control.attach(reader)
        finished=threading.Event()
        def blocked_read():
            try:reader.recv(1)
            except OSError:pass
            finally:finished.set()
        thread=threading.Thread(target=blocked_read);thread.start()
        try:
            control.set()
            self.assertTrue(finished.wait(1))
            self.assertEqual(reader.fileno(),-1)
        finally:
            writer.close();control.detach(reader);thread.join(timeout=2)

    def test_failed_discovery_does_not_hot_retry_or_block_other_transfers(self):
        save_device(self.root,self.device);first=self.create();second=self.create()
        class Sometimes(self.client()):
            checks=0
            def request(self,*args):
                Sometimes.checks+=1
                if Sometimes.checks==1:raise OSError('unreachable')
                return {'status':'ready'},'b'*64
        worker=PushWorker(self.root)
        with patch('tablet_push.PushClient',Sometimes),patch('panelyra.services.tablet_discovery.discover',side_effect=OSError('discovery failed')),patch('tablet_push.deliver') as send:
            worker.run()
        self.assertEqual(self.store.get(first['transfer_id'])['status'],'failed')
        send.assert_called_once_with(self.root,second['transfer_id'],self.device,worker.stopped)

if __name__ == '__main__': unittest.main()
