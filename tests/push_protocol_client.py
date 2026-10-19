"""Cross-language fixture: Python sender talks to the actual Kotlin ReceiveServer."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tablet_push import PushClient, PushError

address, code, source = sys.argv[1:]
file = Path(source)
client = PushClient(address)
# Wrong code fails the server's proof check before credentials or files are sent.
try:
    client.pair('AAAAAAAAAAAA')
    raise AssertionError('Accepted wrong code')
except PushError:
    pass
paired = client.pair(code)
assert paired['token'] and paired['pin']
meta = dict(kind='chapter', series_key='push-book', chapter='1.5', series_title='Push Book', title='Chapter 1.5',
            size=file.stat().st_size, sha256=hashlib.sha256(file.read_bytes()).hexdigest())
offer, _ = client.request('POST', '/v1/offer', meta)
# Drop a transfer after a prefix; retry must resume its exact partial offset.
stream, _ = client.connect()
prefix = file.read_bytes()[:min(73, meta['size'] // 2)]
header = f"PUT /v1/upload/{offer['id']} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {client.token}\r\nX-Offset: 0\r\nContent-Length: {meta['size']}\r\nConnection: close\r\n\r\n"
stream.sendall(header.encode() + prefix)
stream.close()
offer, _ = client.request('POST', '/v1/offer', meta)
assert offer['offset'] == len(prefix), offer
client.send_file(file, meta, lambda n: None, lambda: None)
assert client.request('POST', '/v1/offer', meta)[0]['ready'] is True
# Duplicate delivery is acknowledged without overwriting the chapter/progress.
client.send_file(file, meta, lambda n: None, lambda: None)
# Invalid identity and credentials must not be accepted.
for bad in [dict(meta, series_key='../escape'), dict(meta, size=-1)]:
    try:
        client.request('POST', '/v1/offer', bad)
        raise AssertionError('Accepted bad manifest')
    except PushError:
        pass
try:
    PushClient(address, '0' * 64, client.token).request('GET', '/v1/status')
    raise AssertionError('Accepted different certificate')
except PushError:
    pass
try:
    PushClient(address, client.pin, '0' * 64).request('POST', '/v1/offer', meta)
    raise AssertionError('Accepted bad token')
except PushError:
    pass
print(json.dumps({'token': paired['token'], 'pin': paired['pin']}))
