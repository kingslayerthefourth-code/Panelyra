"""Bounded HTTP page transfer with validator-bound partial-byte reuse.

No validator means no range resume. A changed version is restarted, never appended
to old bytes. Only fully decoded images may leave this app-owned staging cache.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from panelyra.services.library_store import MAX_PAGE_BYTES, decode_image


def fetch_page(session, url, headers, timeout, cache):
    from automation_runtime import atomic_write_json
    base = Path(cache) / hashlib.sha256(url.encode()).hexdigest()
    base.parent.mkdir(parents=True, exist_ok=True)
    part, meta = base.with_suffix('.part'), base.with_suffix('.json')
    try:
        state = json.loads(meta.read_text())
    except (OSError, ValueError):
        state = {}
    validator = state.get('etag') or state.get('modified')
    offset = part.stat().st_size if part.is_file() and validator else 0
    if offset > MAX_PAGE_BYTES or state.get('url') != url:
        offset = 0
    if offset and state.get('complete') and hashlib.sha256(part.read_bytes()).hexdigest() != state.get('sha256'):
        offset = 0
    request_headers = dict(headers, **{'Accept-Encoding': 'identity'})
    if offset and state.get('complete'):
        request_headers['If-None-Match' if state.get('etag') else 'If-Modified-Since'] = validator
    elif offset:
        request_headers.update({'Range': f'bytes={offset}-', 'If-Range': validator})
    for attempt in range(2):
        with session.get(url, headers=request_headers, timeout=timeout, stream=True) as response:
            if response.status_code == 304 and offset and state.get('complete'):
                payload = part.read_bytes()
                decode_image(payload)
                return payload
            response.raise_for_status()
            etag = response.headers.get('ETag', '')
            if etag.startswith('W/'):
                etag = ''  # Weak tags cannot authorize byte-range assembly.
            modified = response.headers.get('Last-Modified', '')
            if response.status_code == 206:
                content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                same_version = validator and validator == (etag or modified)
                if not content_range or int(content_range[1]) != offset or not same_version:
                    if attempt:
                        raise ValueError('Source returned an inconsistent page version/range.')
                    offset = 0
                    request_headers = dict(headers, **{'Accept-Encoding': 'identity'})
                    continue
                total = int(content_range[3])
                if int(content_range[2]) != total - 1:
                    raise ValueError('Source returned an incomplete page range.')
            elif response.status_code == 200:
                offset = 0  # Server ignored If-Range, usually because the version changed.
                total = int(response.headers.get('Content-Length', 0))
            else:
                raise ValueError(f'Unexpected page response: {response.status_code}')
            if total < 0 or total > MAX_PAGE_BYTES:
                raise ValueError('Page exceeds the encoded-image size limit.')
            state = dict(url=url, etag=etag, modified=modified, total=total, complete=False)
            atomic_write_json(meta, state)
            received = offset
            with part.open('ab' if offset else 'wb') as output:
                try:
                    for chunk in response.iter_content(256 * 1024):
                        received += len(chunk)
                        if received > MAX_PAGE_BYTES or (total and received > total):
                            raise ValueError('Source exceeded its declared page size.')
                        output.write(chunk)
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            if total and received != total:
                raise ValueError('Source page ended before its declared size.')
            payload = part.read_bytes()
            decode_image(payload)
            atomic_write_json(meta, dict(state, complete=True, sha256=hashlib.sha256(payload).hexdigest()))
            return payload
    raise ValueError('Source page could not be resumed safely.')
