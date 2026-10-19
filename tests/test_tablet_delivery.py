import hashlib
from contextlib import contextmanager
import json
import ssl
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import http.client
import zipfile
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import sync_library
from sync_library import OriginalLibrary, _valid_cbz
from tablet_delivery import DeliveryStore
import tablet_companion as companion


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / 'page.png'
        Image.new('RGB', (40, 60), '#7645ac').save(self.image)
        for series in ('book-a', 'book-b'):
            for chapter in range(1, 6):
                folder = self.root / f'manhwa/{series}/{chapter:02}'
                folder.mkdir(parents=True)
                (folder / 'metadata.json').write_text(json.dumps({'series_title': 'Same title', 'url': f'https://{series}.test/chapter-{chapter}', 'image_count':3,'language':'en'}))
                with zipfile.ZipFile(folder / 'chapter-pages.cbz', 'w') as archive:
                    for name in ('page10.png', 'page2.png', 'page1.png'):
                        archive.writestr(zipfile.ZipInfo(name, (2025, 1, 1, 0, 0, 0)), self.image.read_bytes())
        covers = self.root / 'data/reader_covers'
        covers.mkdir(parents=True)
        Image.new('RGB', (40, 60), '#b4a1e3').save(covers / 'book-a.png')
        self.library = OriginalLibrary(self.root)
        self.store = DeliveryStore(self.root, self.library)

    def tearDown(self): self.temporary.cleanup()

    def test_cached_catalog_identity_and_cover_versions(self):
        start = time.perf_counter(); catalog = self.library.catalog(); cold = time.perf_counter() - start
        with patch.object(sync_library, '_valid_cbz', side_effect=AssertionError('Unchanged CBZ was revalidated')):
            start = time.perf_counter(); again = self.library.catalog(); warm = time.perf_counter() - start
        self.assertEqual(catalog, again)
        self.assertEqual(len(catalog['chapters']), 10)
        self.assertEqual(len({item['series_key'] for item in catalog['chapters']}), 2)
        self.assertEqual({item['library_key'] for item in catalog['chapters']}, {'book-a', 'book-b'})
        self.assertEqual(catalog['chapters'][0]['chapter'], '1')
        self.assertEqual(catalog['chapters'][0]['cover_provenance'], 'supplied')
        self.assertEqual(len({item['sha256'] for item in catalog['chapters']}), 1)
        old = catalog['chapters'][0]['cover_version']
        Image.new('RGB', (40, 60), '#010101').save(self.root / 'data/reader_covers/book-a.png')
        self.assertNotEqual(old, self.library.catalog()['chapters'][0]['cover_version'])
        print(f'Catalog cold={cold*1000:.2f}ms warm={warm*1000:.2f}ms')

    def test_selected_durable_queue_ack_pause_cancel_and_missing(self):
        transfer = self.store.create([{'series_key': 'book-a', 'chapter': str(i)} for i in range(1, 6)])
        identity = transfer['transfer_id']
        self.assertEqual(len(transfer['chapters']), 5)
        self.assertNotIn('files', transfer)
        self.assertEqual(DeliveryStore(self.root).list()[0]['transfer_id'], identity)
        self.store.update(identity, 'pause', {})
        with self.assertRaises(ValueError): self.store.file(identity, 'book-a', '1')
        self.store.update(identity, 'resume', {})
        with self.assertRaises(ValueError): self.store.update(identity, 'ack', {'series_key': 'book-a', 'chapter': '1', 'sha256': 'wrong'})
        inventory = {}
        for item in transfer['chapters']:
            body = {'series_key': item['series_key'], 'chapter': item['chapter'], 'sha256': item['sha256']}
            self.store.update(identity, 'ack', body); self.store.update(identity, 'ack', body)
            inventory[f"{item['series_key']}/{item['chapter']}"] = item['sha256']
            inventory[f"cover/{item['series_key']}"] = item['cover_version']
        self.assertEqual(self.store.get(identity)['status'], 'ready')
        self.store.tablet({'inventory': inventory})
        with self.assertRaises(ValueError): self.store.create([{'series_key': 'book-a', 'chapter': '01'}], missing=True)
        second = self.store.create([{'series_key': 'book-b', 'chapter': '1'}])
        self.store.update(second['transfer_id'], 'cancel', {})
        self.store.update(second['transfer_id'], 'resume', {})
        self.assertEqual(self.store.get(second['transfer_id'])['status'], 'cancelled')

    def test_changed_file_and_invalid_archives_are_rejected(self):
        transfer = self.store.create([{'series_key': 'book-a', 'chapter': '1'}])
        key = transfer['chapters'][0]['series_key']
        file, _ = self.store.file(transfer['transfer_id'], key, '1')
        file.write_bytes(b'invalid archive')
        with self.assertRaises(ValueError): self.store.file(transfer['transfer_id'], key, '1')
        duplicate = self.root / 'duplicate.cbz'
        with zipfile.ZipFile(duplicate, 'w') as archive:
            for name in ('page.png', 'PAGE.png'): archive.writestr(name, self.image.read_bytes())
        self.assertFalse(_valid_cbz(duplicate))
        self.assertTrue(any(error['chapter'] == '01' for error in self.library.catalog()['errors']))

    def test_authenticated_https_selected_delivery_and_range_resume(self):
        names = {'STATE_DIR': self.root / 'certs', 'CERT_FILE': self.root / 'certs/cert.pem',
                 'KEY_FILE': self.root / 'certs/key.pem', 'CERT_HOST_FILE': self.root / 'certs/host',
                 'PAIR_CODE_FILE': self.root / 'certs/pair.json'}
        with patch.multiple(companion, **names):
            companion.ensure_certificate('127.0.0.1')
            pin = companion.certificate_fingerprint()
            companion.ensure_certificate('192.168.99.1')
            self.assertEqual(pin, companion.certificate_fingerprint())
            state = companion.CompanionState(app_dir=self.root)
            token = state.pair(state.pairing_secret, 'Test tablet')
            server = companion.CompanionServer(('127.0.0.1', 0), state)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(names['CERT_FILE'], names['KEY_FILE'])
            server.socket = ssl_context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.check_hostname = False
            trust.verify_mode = ssl.CERT_NONE
            base = f'https://127.0.0.1:{server.server_port}'
            served = ssl.get_server_certificate(('127.0.0.1', server.server_port))
            self.assertEqual(pin, hashlib.sha256(ssl.PEM_cert_to_DER_cert(served)).hexdigest(), 'Server did not present its pinned fixture certificate')
            @contextmanager
            def request(path, body=None, headers=None, authorized=True):
                headers = headers or {}
                headers['Connection'] = 'close'
                if authorized: headers['Authorization'] = f'Bearer {token}'
                if body is not None: headers['Content-Type'] = 'application/json'
                connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=trust)
                try:
                    connection.connect()
                    # Match Android: authenticate this exact certificate before sending credentials.
                    self.assertEqual(pin, hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest())
                    connection.request('POST' if body is not None else 'GET', path, json.dumps(body).encode() if body is not None else None, headers)
                    with connection.getresponse() as response:
                        if response.status >= 400:
                            raise urllib.error.HTTPError(path, response.status, response.reason, response.headers, None)
                        yield response
                finally:
                    connection.close()
            try:
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    with request('/api/v3/transfers/pending', authorized=False): pass
                self.assertEqual(unauthorized.exception.code, 401)
                with request('/api/v3/transfers', {'chapters': [{'series_key': 'book-a', 'chapter': '1'}]}) as response:
                    transfer = json.load(response)
                item = transfer['chapters'][0]
                with request('/api/v3/transfers/pending') as response: self.assertEqual(len(json.load(response)['transfers']), 1)
                with request(item['download_url']) as response: content = response.read()
                prefix = content[:100]
                with request(item['download_url'], headers={'Range': 'bytes=100-', 'If-Range': f"\"{item['sha256']}\""}) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(hashlib.sha256(prefix + response.read()).hexdigest(), item['sha256'])
                with request(item['cover_url']) as response: self.assertEqual(hashlib.sha256(response.read()).hexdigest(), item['cover_version'])
                with request(f"/api/v3/transfers/{transfer['transfer_id']}/ack", {'series_key': item['series_key'], 'chapter': '1', 'sha256': item['sha256']}) as response:
                    self.assertEqual(json.load(response)['status'], 'ready')
            finally: server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
