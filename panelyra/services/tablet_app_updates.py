"""Offer explicitly published APKs to the paired tablet; never claim installation."""
import hashlib
import json
import os
import time
from pathlib import Path
from panelyra.services.library_store import connect, db_path

_HASHES = {}

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as out:
        json.dump(value, out, indent=2); out.flush(); os.fsync(out.fileno())
    os.chmod(temporary, 0o600); os.replace(temporary, path)

def published(root):
    manifest = Path(root) / 'releases/tablet-release.json'
    if not manifest.is_file(): return None
    value = json.loads(manifest.read_text())
    if not isinstance(value, dict): raise ValueError('Invalid release manifest')
    name = value.get('filename', '')
    if not __import__('re').fullmatch(r'Panelyra-Tablet-v\d+\.\d+\.\d+-debug\.apk', name): raise ValueError('Invalid released APK filename')
    path = (manifest.parent / name).resolve()
    if path.parent != manifest.parent.resolve() or value.get('application_id') != 'com.panelyra.tablet': raise ValueError('Invalid update identity')
    if type(value.get('version_code')) is not int or value['version_code'] < 1: raise ValueError('Invalid released APK version')
    stat = path.stat(); signature = (str(path), stat.st_size, stat.st_mtime_ns)
    if not 1 <= stat.st_size <= 200_000_000 or stat.st_size != value.get('size'): raise ValueError('Released APK size changed')
    if signature not in _HASHES:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(256 * 1024), b''): digest.update(block)
        _HASHES.clear(); _HASHES[signature] = digest.hexdigest()
    if _HASHES[signature] != value.get('sha256'): raise ValueError('Released APK checksum changed')
    return value, path

def offer_latest(root, device, control):
    from tablet_push import PushClient
    if not device or control.is_set(): return
    with connect(db_path(root)) as db:
        row = db.execute("SELECT value FROM settings WHERE key='tablet_auto_updates'").fetchone()
    if not row or json.loads(row[0]) is not True: return
    state_path = Path(root) / 'data/tablet_companion/app-update.json'
    try: previous = json.loads(state_path.read_text())
    except (OSError, ValueError): previous = {}
    if previous.get('status') == 'invalid_release' and time.time() - previous.get('checked_at', 0) < 60: return
    try: release = published(root)
    except (OSError, ValueError, TypeError, KeyError) as error:
        atomic(state_path, {'status':'invalid_release', 'checked_at':time.time(), 'error':str(error)[:200]}); return
    if release is None: return
    info, file = release
    if previous.get('sha256') == info['sha256'] and time.time() - previous.get('checked_at', 0) < 60: return
    state = {'checked_at': time.time(), 'sha256': info['sha256'], 'version_name': info['version_name'], 'version_code': info['version_code']}
    # Persist the attempt before networking: an unreachable tablet never causes a hot retry loop.
    atomic(state_path, dict(state, status='checking'))
    try:
        client = PushClient(device['address'], device['pin'], device['token'], control)
        status, _ = client.request('GET', '/v1/status')
        if status.get('app_version_code', 0) >= info['version_code']:
            state['status'] = 'installed'
        elif not status.get('apk_updates'):
            state.update(status='waiting_for_tablet_support', note='Install the initial app update once; or enable automatic update downloads on the tablet.')
        elif status.get('update_received_code', 0) >= info['version_code']:
            state['status'] = 'downloaded_awaiting_android_approval'
        else:
            meta = {'kind':'apk', 'series_key':'panelyra-tablet-app', 'size':info['size'], 'sha256':info['sha256'],
                    'version_code':info['version_code'], 'version_name':info['version_name'], 'title':f"Panelyra {info['version_name']} update"}
            def checkpoint():
                from panelyra.engine import policy
                if control.is_set() or policy(root):
                    from tablet_push import DeliveryStopped
                    raise DeliveryStopped()
            last = [0.0]
            def progress(sent):
                if time.monotonic() - last[0] > 1:
                    last[0] = time.monotonic(); atomic(state_path, dict(state, status='sending', sent_bytes=sent, total_bytes=info['size']))
            client.send_file(file, meta, progress, checkpoint)
            state['status'] = 'downloaded_awaiting_android_approval'
    except Exception as error:
        state.update(status='waiting', error=str(error)[:200])
    atomic(state_path, state)
