import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelyra.services.library_store import transaction, db_path
from panelyra.services.tablet_app_updates import offer_latest, published

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        folder = self.root / 'releases'; folder.mkdir(); self.file = folder / 'Panelyra-Tablet-v1.8.0-debug.apk'; self.file.write_bytes(b'fixture-apk')
        info = {'filename':self.file.name,'application_id':'com.panelyra.tablet','version_code':16,'version_name':'1.8.0','size':self.file.stat().st_size,'sha256':hashlib.sha256(self.file.read_bytes()).hexdigest()}
        (folder/'tablet-release.json').write_text(json.dumps(info))
        with transaction(db_path(self.root)) as db: db.execute("INSERT INTO settings VALUES('tablet_auto_updates','true')")
        self.state = self.root / 'data/tablet_companion/app-update.json'; self.calls = []
    def client(self, status):
        owner = self
        class Client:
            def __init__(self, *args): pass
            def request(self, *args): return status, 'pin'
            def send_file(self, file, metadata, progress, checkpoint):
                checkpoint(); owner.calls.append(metadata); progress(file.stat().st_size)
        return Client
    def test_bootstrap_does_not_send_apk_to_an_old_receiver(self):
        with patch('tablet_push.PushClient', self.client({'status':'ready'})): offer_latest(self.root, {'address':'tablet','pin':'pin','token':'token'}, threading.Event())
        self.assertFalse(self.calls); self.assertEqual(json.loads(self.state.read_text())['status'], 'waiting_for_tablet_support')
    def test_receipt_is_not_installation_and_offers_are_throttled(self):
        with patch('tablet_push.PushClient', self.client({'apk_updates':True,'app_version_code':15,'update_received_code':0})):
            offer_latest(self.root, {'address':'tablet','pin':'pin','token':'token'}, threading.Event())
            offer_latest(self.root, {'address':'tablet','pin':'pin','token':'token'}, threading.Event())
        self.assertEqual(len(self.calls),1); self.assertEqual(self.calls[0]['kind'],'apk')
        self.assertEqual(json.loads(self.state.read_text())['status'],'downloaded_awaiting_android_approval')
    def test_only_published_unchanged_apks_are_offered(self):
        self.file.write_bytes(b'changed')
        with self.assertRaises(ValueError): published(self.root)
        (self.root/'releases/tablet-release.json').write_text(json.dumps({'filename':'../other.apk'}))
        with self.assertRaises(ValueError): published(self.root)

if __name__=='__main__': unittest.main()
