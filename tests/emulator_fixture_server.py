"""Isolated authenticated fixture for real APK acceptance tests. No user library is used."""
import json
import ssl
import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tablet_companion as companion
from tablet_delivery import DeliveryStore
from PIL import Image
import zipfile

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
companion.STATE_DIR = root / 'data/tablet_companion'
companion.CERT_FILE = companion.STATE_DIR / 'certificate.pem'
companion.KEY_FILE = companion.STATE_DIR / 'key.pem'
companion.CERT_HOST_FILE = companion.STATE_DIR / 'certificate-host.txt'
companion.PAIR_CODE_FILE = companion.STATE_DIR / 'pair-code.json'
companion.ensure_certificate('127.0.0.1')
for chapter in range(1, 6):
    folder = root / f'manhwa/fixture-book/{chapter:02}'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'metadata.json').write_text(json.dumps({'series_title': 'Fixture Book', 'url': f'https://fixture.test/book/chapter-{chapter}'}))
    with zipfile.ZipFile(folder / 'chapter-pages.cbz', 'w') as archive:
        for page in (10, 2, 1):
            image = folder / f'page{page}.png'
            Image.new('RGB', (800, 1200), ['#7450aa', '#bba0ec', '#302a3e'][page % 3]).save(image)
            archive.write(image, image.name)
covers = root / 'data/reader_covers'; covers.mkdir(parents=True, exist_ok=True)
Image.new('RGB', (320, 480), '#7450aa').save(covers / 'fixture-book.png')
state = companion.CompanionState(app_dir=root)
state.deliveries.create([{'series_key': 'fixture-book', 'chapter': str(i)} for i in range(1, 6)])
server = companion.CompanionServer(('127.0.0.1', 19443), state)
challenge = companion.PairChallengeServer(('127.0.0.1', 19444), state)
threading.Thread(target=challenge.serve_forever, daemon=True).start()
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(companion.CERT_FILE, companion.KEY_FILE)
server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
code, _ = companion.current_pair_code()
(root / 'acceptance.json').write_text(json.dumps({'address': '10.0.2.2:19443', 'code': code}))
print('Isolated authenticated emulator fixture is ready', flush=True)
server.serve_forever()
