"""Shared local catalog. Files stay private; HTTP serializes an explicit public model."""
from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path
from PIL import Image

SUFFIXES={'.jpg','.jpeg','.png','.webp','.gif','.avif'}

def read_json(path, default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {} if default is None else default

def number(value):
    match=re.fullmatch(r'(?:chapter[-_ ]*)?(\d+(?:\.\d+)?)',str(value),re.I)
    return float(match[1]) if match else None

def sort_key(value):
    from panelyra.services.library_store import natural_key
    from decimal import Decimal
    if re.fullmatch(r'\d+(?:\.\d+)?', str(value)):
        return (0,Decimal(str(value)))
    return (1,natural_key(value))

@lru_cache(maxsize=12000)
def dimensions(path, size, modified):
    try:
        from panelyra.services.library_store import decode_image
        return decode_image(Path(path).read_bytes())
    except (OSError,ValueError):return (0,0)

def image_size(path):
    try:
        stat=path.stat();return dimensions(str(path),stat.st_size,stat.st_mtime_ns)
    except OSError:return (0,0)

def catalog(root: Path):
    from panelyra.services.library_store import LibraryStore
    return LibraryStore(root).catalog()

def audit(root):
    return [{'series':s,'edition':e,'chapter':n,'reason':c['error'],'status':c['status']} for s,entry in catalog(root).items() for e,chapters in entry['editions'].items() for n,c in chapters.items() if not c['verified'] or not c['healthy']]
