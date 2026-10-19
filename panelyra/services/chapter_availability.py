"""Conservative public-link/locked chapter discovery; never unlocks content."""
from __future__ import annotations
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from panelyra.services.chapter_counts import _fetch, _same_source, parse_chapter_count
from panelyra.services.download_identity import chapter_key
_CACHE = {}


def parse_catalog(html: str, url: str, source: str, *, full_list=False) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True).casefold()
    if any(marker in text for marker in ('just a moment', 'verify you are human', 'access denied', 'checking your browser')):
        return {'chapters':{},'complete':False,'notice':'Source requires browser verification. Availability is unverified.'}
    scope = soup.select_one('#chapter-list, #chapterlist, #chapters, .listing-chapters_wrap, .chapters')
    scoped = scope is not None
    scope = scope if scoped else soup
    path = urlsplit(url).path.rstrip('/')
    rows = {}
    candidates = list(scope.select('a[href]')) + list(scope.select('li[data-num], [data-chapter-number], .ch-item'))
    for node in candidates:
        anchor = node if node.name == 'a' else node.select_one('a[href]')
        target = _same_source(anchor.get('href',''),url) if anchor else ''
        target_path = urlsplit(target).path
        label = node.get_text(' ',strip=True)
        match = re.search(r'\b(?:chapter|chap|ch)\.?\s*(\d+(?:\.\d+)?)',label,re.I)
        raw = node.get('data-num') or node.get('data-chapter-number') or (match.group(1) if match else '')
        if not raw:
            match = re.search(r'(?:chapter|ch)[-/](\d+(?:\.\d+)?)',target_path,re.I)
            raw = match.group(1) if match else ''
        if not raw:
            continue
        try:
            number = chapter_key(raw)
        except ValueError:
            continue
        belongs = target_path.startswith(path + '/') and 'chapter' in target_path
        if scoped and target and target_path != path:
            belongs = True
        if source in {'MangaTaro','Rolia'}:
            belongs = target_path.startswith('/read/'+path.rsplit('/',1)[-1]+'/')
        if source == 'WeebCentral' and target_path.startswith('/chapters/'):
            belongs = True
        if not scoped and not belongs:
            continue
        wrapper = node.find_parent('li') if node.name == 'a' else node
        wrapper = wrapper if wrapper is not None else node
        locked = bool(wrapper.select_one('.lucide-lock, [data-lucide="lock"], [data-locked="true"]')) or bool(re.search(r'\b(?:locked|unlock|premium|coins?)\b',wrapper.get_text(' ',strip=True),re.I)) or 'locked' in wrapper.get('class',[])
        if not target or target_path == '/':
            status = 'locked' if locked else 'unknown'
        else:
            status = 'locked' if locked else 'public_link'
        if target and not belongs:
            continue
        entry = {'number':number,'url':target if belongs else '', 'status':status}
        previous = rows.get(number)
        # Prefer a public linked row over a duplicate decorative/locked row.
        if previous is None or (status == 'public_link' and previous['status'] != 'public_link'):
            rows[number] = entry
    count = parse_chapter_count(html,url,source)
    total = count.get('chapter_count')
    known_total = count.get('chapter_count_kind') == 'reported'
    complete = bool(full_list or (known_total and total == len(rows)))
    return {'chapters':dict(sorted(rows.items(),key=lambda item:Decimal(item[0]))),'complete':complete,'total':total,'notice':'' if rows else 'This source did not expose a chapter list. Availability is unverified.'}


def get_catalog(url: str, source: str = '', *, refresh=False, on_progress=None) -> dict:
    from panelyra.services.title_search import SOURCES
    if not source:
        source = next((name for name,base in SOURCES.items() if urlsplit(base).hostname == urlsplit(url).hostname),'')
    if source not in SOURCES or urlsplit(url).hostname != urlsplit(SOURCES[source]).hostname:
        return {'chapters':{},'complete':False,'notice':'This source has no verified chapter-list adapter. You can still queue explicit chapter numbers.'}
    cached = _CACHE.get(url)
    if cached and not refresh and time.monotonic()-cached[0] < 300:
        return cached[1]
    try:
        if on_progress: on_progress('Connecting to the source…')
        html = _fetch(url)
        full = False
        if source == 'WeebCentral':
            soup = BeautifulSoup(html,'html.parser')
            link = soup.select_one('[hx-get$="/full-chapter-list"]')
            target = _same_source(link.get('hx-get',''),url) if link else ''
            if target and urlsplit(target).path == urlsplit(url).path.rsplit('/',1)[0]+'/full-chapter-list':
                if on_progress: on_progress('Loading the full chapter list…')
                html = _fetch(target); full = True
        if on_progress: on_progress('Sorting chapters and checking public links…')
        result = parse_catalog(html,url,source,full_list=full)
    except Exception:
        result = {'chapters':{},'complete':False,'notice':'Chapter list could not be reached. Availability is unverified.'}
    _CACHE[url] = (time.monotonic(),result)
    if on_progress: on_progress(f"{len(result.get('chapters', {}))} chapters found")
    return result


def chapter_status(catalog: dict, number: str) -> dict:
    return dict(catalog.get('chapters',{}).get(number) or {'number':number,'url':'','status':'missing' if catalog.get('complete') else 'unknown'})
