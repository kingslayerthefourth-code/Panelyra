"""Mac-initiated, certificate-pinned chapter delivery. Never opens a Mac listener."""
from __future__ import annotations

import hashlib
import fcntl
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from tablet_delivery import DeliveryStore


class PushError(RuntimeError):
    pass


class TransferControl(threading.Event):
    """Interrupt a blocked socket, not just the next upload chunk."""
    def __init__(self):
        super().__init__();self.lock=threading.Lock();self.streams=set()

    def attach(self,stream):
        with self.lock:
            if self.is_set():
                stream.close();raise DeliveryStopped()
            self.streams.add(stream)

    def detach(self,stream):
        with self.lock:self.streams.discard(stream)

    def set(self):
        super().set()
        with self.lock:
            for stream in self.streams:
                try:stream.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                stream.close()


def endpoint(address: str) -> tuple[str, int]:
    value = urlsplit(address.strip() if '://' in address else 'https://' + address.strip())
    if value.scheme != 'https' or value.username or value.password or value.path not in ('', '/') or value.query or value.fragment:
        raise ValueError('Enter the tablet Wi-Fi IP address and port shown in Receive mode.')
    try:
        host = str(ipaddress.IPv4Address(value.hostname or ''))
        port = value.port or 8590
    except ValueError:
        raise ValueError('Enter the tablet Wi-Fi IPv4 address, for example 192.168.1.20:8590.') from None
    ip = ipaddress.ip_address(host)
    if not (ip.is_private or ip.is_link_local) or ip.is_unspecified or ip.is_multicast or not 1 <= port <= 65535:
        raise ValueError('Use a local tablet address.')
    return host, port


def proof(code: str, role: str, client: str, server: str, pin: str) -> str:
    return hmac.new(code.encode(), f'panelyra-push-v1|{role}|{client}|{server}|{pin}'.encode(), hashlib.sha256).hexdigest()


class PushClient:
    def __init__(self, address: str, pin: str = '', token: str = '', control=None):
        self.host, self.port = endpoint(address)
        self.address = f'{self.host}:{self.port}'
        self.pin, self.token = pin, token
        self.control=control

    @contextmanager
    def connection(self):
        stream,pin=self.connect()
        try:
            if self.control is not None:self.control.attach(stream)
            yield stream,pin
        finally:
            if self.control is not None:self.control.detach(stream)
            stream.close()

    def connect(self) -> tuple[ssl.SSLSocket, str]:
        # Self-signed identity: pin checked BEFORE sending any token or chapter data.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        raw = socket.create_connection((self.host, self.port), timeout=8)
        try:
            stream = context.wrap_socket(raw, server_hostname=self.host)
            stream.settimeout(120)
            actual = hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()
            if self.pin and not hmac.compare_digest(actual, self.pin):
                stream.close()
                raise PushError('Tablet identity changed. Check the tablet address; reconnect with a new code only if you reset its pairing.')
            return stream, actual
        except Exception:
            raw.close()
            raise

    @staticmethod
    def response(stream: ssl.SSLSocket) -> dict:
        response = http.client.HTTPResponse(stream)
        response.begin()
        data = response.read(65537)
        if len(data) > 65536:
            raise PushError('Tablet returned an oversized response.')
        try:
            result = json.loads(data)
            if not isinstance(result, dict): raise ValueError()
        except ValueError:
            raise PushError('Tablet returned an invalid response.') from None
        if response.status != 200:
            raise PushError(str(result.get('error', f'Tablet returned {response.status}'))[:300])
        return result

    def request(self, method: str, path: str, body: dict | None = None, *, authenticated: bool = True) -> tuple[dict, str]:
        if authenticated and (not self.pin or not re.fullmatch('[a-f0-9]{64}', self.token)):
            raise PushError('Connect to the tablet first.')
        data = json.dumps(body).encode() if body is not None else b''
        with self.connection() as (stream,pin):
            headers = f'{method} {path} HTTP/1.1\r\nHost: {self.address}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n'
            if authenticated: headers += f'Authorization: Bearer {self.token}\r\n'
            stream.sendall(headers.encode() + b'\r\n' + data)
            return self.response(stream), pin

    def pair(self, code: str) -> dict:
        code = re.sub(r'[\s-]', '', code).upper()
        if not re.fullmatch('[A-Z2-9]{12}', code): raise ValueError('Enter the 12-character code shown on the tablet.')
        nonce = secrets.token_hex(32)
        result, pin = self.request('POST', '/v1/pair/start', {'client_nonce': nonce}, authenticated=False)
        server = str(result.get('server_nonce', ''))
        if not re.fullmatch('[a-f0-9]{64}', server) or not hmac.compare_digest(str(result.get('proof', '')), proof(code, 'server', nonce, server, pin)):
            raise PushError('The tablet code or identity did not match. No chapters were sent.')
        self.pin = pin
        result, _ = self.request('POST', '/v1/pair/finish', {'client_nonce': nonce, 'server_nonce': server,
                                                         'proof': proof(code, 'client', nonce, server, pin)}, authenticated=False)
        if not re.fullmatch('[a-f0-9]{64}', str(result.get('token', ''))): raise PushError('Invalid tablet pairing response.')
        self.token = result['token']
        return {'address': self.address, 'pin': pin, 'token': self.token, 'device': str(result.get('device', 'Tablet'))[:100]}

    def send_file(self, file: Path, metadata: dict, progress, checkpoint) -> None:
        offer, _ = self.request('POST', '/v1/offer', metadata)
        identity, offset = str(offer.get('id', '')), offer.get('offset')
        if not re.fullmatch('[a-f0-9]{64}', identity) or type(offset) is not int or not 0 <= offset <= metadata['size']:
            raise PushError('Invalid tablet transfer position.')
        if offer.get('ready') is True:
            progress(metadata['size']); return
        progress(offset)
        checkpoint()
        if offset < metadata['size']:
            with self.connection() as (stream,_), file.open('rb') as source:
                source.seek(offset)
                headers = f'PUT /v1/upload/{identity} HTTP/1.1\r\nHost: {self.address}\r\nAuthorization: Bearer {self.token}\r\nX-Offset: {offset}\r\nContent-Length: {metadata["size"] - offset}\r\nConnection: close\r\n\r\n'
                stream.sendall(headers.encode())
                sent = offset
                while sent < metadata['size']:
                    checkpoint()
                    block = source.read(min(256 * 1024, metadata['size'] - sent))
                    if not block: raise PushError('Chapter file changed while sending.')
                    stream.sendall(block); sent += len(block); progress(sent)
                self.response(stream)
        checkpoint()
        done, _ = self.request('POST', f'/v1/commit/{identity}', {})
        if done.get('ready') is not True: raise PushError('Tablet has not confirmed chapter validation.')
        progress(metadata['size'])


def config_path(root: Path) -> Path:
    return root / 'data/tablet_companion/push-device.json'


def load_device(root: Path) -> dict:
    try:
        data = json.loads(config_path(root).read_text())
        if not isinstance(data, dict): return {}
        endpoint(data['address'])
        if not all(re.fullmatch('[a-f0-9]{64}', data[key]) for key in ('pin', 'token')): return {}
        return data
    except (OSError, ValueError, KeyError, TypeError): return {}


def save_device(root: Path, device: dict) -> None:
    path = config_path(root); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
            json.dump(device, out); out.flush(); os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class DeliveryStopped(Exception):
    pass


def deliver(root: Path, identity: str, device: dict, stopped=None) -> None:
    store = DeliveryStore(root)
    client = PushClient(device['address'], device['pin'], device['token'], stopped)
    value = store.get(identity)
    if value.get('transport') != 'push': raise ValueError('Not a Mac-send transfer')
    started, last = time.monotonic(), 0.0
    completed = 0;network_bytes=0;observed={'last':None}
    def checkpoint():
        from panelyra.engine import policy
        if policy(root) or (stopped is not None and stopped.is_set()): raise DeliveryStopped()
        if store.status(identity) in ('paused', 'cancelled'): raise DeliveryStopped()
    def update(count: int):
        nonlocal last,network_bytes
        if observed['last'] is not None:network_bytes+=max(0,count-observed['last'])
        observed['last']=count
        now = time.monotonic()
        if now - last < .5: return
        last = now
        speed=int(network_bytes/max(.1,now-started));remaining=max(0,value['total_bytes']-completed-count)
        store.update(identity, 'progress', {'status': 'receiving', 'received_bytes': completed + count,
                     'bytes_per_second':speed,'eta_seconds':int(remaining/speed) if speed else 0},summary=True)
    try:
        checkpoint()
        if value.get('target_pin') != device['pin']: raise PushError('This delivery belongs to a different tablet. Cancel it and select chapters again.')
        client.request('GET', '/v1/status')
        for chapter in value['chapters']:
            checkpoint()
            key = f"{chapter['series_key']}/{chapter['chapter']}"
            if key in value['acknowledged']:
                completed += chapter['size']; continue
            digest=chapter['sha256'];observed['last']=None
            metadata = {key: chapter[key] for key in ('series_key', 'chapter', 'series_title', 'title', 'sha256', 'size')}
            metadata['source_url'] = chapter.get('source_url','')
            metadata['manifest_version'] = 4
            metadata['legacy_series_keys'] = chapter.get('legacy_series_keys',[])
            metadata['kind'] = 'chapter'
            if not chapter.get('cover_only'):
                file,digest=store.file(identity,chapter['series_key'],chapter['chapter'])
                client.send_file(file, metadata, update, checkpoint)
            store.update(identity, 'progress', {'status': 'verifying', 'received_bytes': completed + chapter['size']},summary=True)
            cover, version = store.file(identity, chapter['series_key'])
            client.send_file(cover, {'kind': 'cover', 'series_key': chapter['series_key'], 'sha256': version,
                                    'size': cover.stat().st_size, 'cover_provenance': chapter.get('cover_provenance', 'supplied')}, lambda n: None, checkpoint)
            checkpoint()
            store.update(identity, 'ack', {'series_key': chapter['series_key'], 'chapter': chapter['chapter'], 'sha256': digest},summary=True)
            completed += chapter['size']
    except DeliveryStopped:
        pass
    except Exception as error:
        if (stopped is None or not stopped.is_set()) and store.status(identity) not in ('paused', 'cancelled'):
            message = str(error) if isinstance(error, (PushError, ValueError)) else 'Tablet unreachable or transfer interrupted. Keep Receive mode on, check Wi-Fi, then Retry.'
            store.update(identity, 'progress', {'status': 'failed', 'error': message},summary=True)


class PushWorker:
    """Engine-owned sender; UI tabs only persist delivery requests."""
    def __init__(self, root: Path):
        self.root, self.lock = root, threading.Lock()
        self.thread = None
        self.stopped = TransferControl()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive(): return
            self.stopped.clear()
            self.thread = threading.Thread(target=self.run, name='panelyra-push', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set()

    def run(self):
        from panelyra.engine import policy
        if policy(self.root): return
        path = self.root/'data/tablet_companion/push-worker.lock'
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a+') as lease:
            try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return
            try: self._run()
            finally: fcntl.flock(lease,fcntl.LOCK_UN)

    def _run(self):
        store = DeliveryStore(self.root)
        # Never spin on failed transfers. A user retries after recovering Wi-Fi.
        for item in store.list(transport='push',states=('queued','receiving','verifying','waiting')):
            if self.stopped.is_set():return
            if item.get('transport') == 'push' and item['status'] in ('queued', 'receiving', 'verifying', 'waiting'):
                device = load_device(self.root)
                if device:
                    try:
                        try:
                            PushClient(device['address'],device['pin'],device['token'],self.stopped).request('GET','/v1/status')
                        except Exception:
                            if self.stopped.is_set():return
                            from panelyra.services.tablet_discovery import discover
                            address=discover(device)
                            if address:
                                # Discovery never replaces TLS pin verification.
                                PushClient(address,device['pin'],device['token'],self.stopped).request('GET','/v1/status')
                                device=dict(device,address=address);save_device(self.root,device)
                        deliver(self.root, item['transfer_id'], device, self.stopped)
                    except Exception as error:
                        if self.stopped.is_set():return
                        if store.status(item['transfer_id']) not in ('paused','cancelled','ready'):
                            store.update(item['transfer_id'],'progress',{'status':'failed','error':str(error)[:300]},summary=True)
        if not self.stopped.is_set():
            from panelyra.services.tablet_app_updates import offer_latest
            offer_latest(self.root, load_device(self.root), self.stopped)
