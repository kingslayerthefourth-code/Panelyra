"""Read-only chapter totals from public series pages, cached for ten minutes."""
from __future__ import annotations
import re
import time
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, BoundedSemaphore
from urllib.parse import urljoin, urlsplit, urlunsplit
import requests
from bs4 import BeautifulSoup

_CACHE = {}
_LOCK = Lock()
_HOST_LIMITS = {}
_UNKNOWN = {"chapter_count": None, "chapter_count_kind": "unknown"}


def _same_source(url: str, base: str) -> str:
    parsed = urlsplit(urljoin(base, url))
    if parsed.scheme != 'https' or parsed.hostname != urlsplit(base).hostname or parsed.username or parsed.password:
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


def _fetch(url: str) -> str:
    from panelyra.services.title_search import HEADERS
    base = url
    for _ in range(3):
        with requests.get(url, headers=HEADERS, timeout=(4, 8), stream=True, allow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                url = _same_source(response.headers.get('Location', ''), base)
                if not url:
                    raise ValueError('Source moved')
                continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 3_000_000:
                    raise ValueError('Series page too large')
            return data.decode('utf-8', errors='replace')
    raise ValueError('Too many redirects')


def parse_chapter_count(html: str, series_url: str, source: str, *, full_list: bool = False) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    # Rolling-number widgets render the same digits several times; use their
    # accessible value, not the hidden animated copies. Ignore recommendation counts.
    for node in soup.select('script, style, noscript, [aria-hidden="true"]'):
        node.decompose()
    text = soup.get_text(' ', strip=True).casefold()
    if any(marker in text for marker in ('just a moment', 'access denied', 'verify you are human', 'checking your browser')):
        return dict(_UNKNOWN)
    declared = None
    for label in soup.find_all(string=re.compile(r'^\s*chapters\s*$', re.I)):
        parent = label.parent
        if parent.find_parent('a'):
            continue
        # Both “201 Chapters” headings and a metadata row “Chapters 78”.
        for node in (parent, parent.parent):
            if node is None:
                continue
            value = node.get_text(' ', strip=True)
            match = re.fullmatch(r'(?:([\d,]+)\s+chapters|chapters\s+([\d,]+))', value, re.I)
            if match:
                count = int((match.group(1) or match.group(2)).replace(',', ''))
                if 0 <= count <= 100000:
                    declared = count
                    break
        if declared is not None:
            break
    # Match a standalone heading when the whole phrase is one text node.
    if declared is None:
        for heading in soup.select('h1,h2,h3,h4'):
            match = re.fullmatch(r'([\d,]+)\s+chapters', heading.get_text(' ', strip=True), re.I)
            if match:
                declared = int(match.group(1).replace(',', ''))
                break
    if declared is not None:
        return {"chapter_count": declared, "chapter_count_kind": "reported"}

    series_path = urlsplit(series_url).path.rstrip('/')
    numbers = set()
    scope = soup.select_one('#chapter-list, #chapterlist, #chapters, .listing-chapters_wrap, .chapters')
    if scope is None:
        scope = soup
    for anchor in scope.select('a[href]'):
        url = _same_source(anchor['href'], series_url)
        if not url:
            continue
        path = urlsplit(url).path
        belongs = path.startswith(series_path + '/')
        if source == 'WeebCentral':
            belongs = bool(re.fullmatch(r'/chapters/[A-Z0-9]{26}/?', path))
        elif source in {'MangaTaro', 'Rolia'}:
            slug = series_path.rsplit('/', 1)[-1]
            belongs = path.startswith('/read/' + slug + '/')
        elif source == 'MangaPill':
            belongs = bool(scope is not soup and path.startswith('/chapters/'))
        if not belongs:
            continue
        label = anchor.get_text(' ', strip=True)
        match = re.search(r'\b(?:chapter|chap|ch)\.?\s*(\d+(?:\.\d+)?)', label, re.I)
        if match is None:
            match = re.search(r'/(?:chapter|ch)[-/](\d+(?:\.\d+)?)', path, re.I)
        if match:
            numbers.add(float(match.group(1)))
    if numbers:
        # Highest chapter number is NOT a chapter count; gaps and specials matter.
        return {"chapter_count": len(numbers), "chapter_count_kind": "listed" if full_list else "partial"}
    return dict(_UNKNOWN)


def get_chapter_count(result: dict) -> dict:
    url, source = result.get('url', ''), result.get('source', '')
    if not url or urlsplit(url).scheme != 'https':
        return dict(_UNKNOWN)
    with _LOCK:
        cached = _CACHE.get(url)
        if cached and time.monotonic() - cached[0] < 600:
            return dict(cached[1])
        host_limit = _HOST_LIMITS.setdefault(urlsplit(url).hostname, BoundedSemaphore(2))
    try:
        with host_limit:
            html = _fetch(url)
            if source == 'WeebCentral':
                # This public endpoint is advertised by the series page itself.
                soup = BeautifulSoup(html, 'html.parser')
                link = soup.select_one('[hx-get$="/full-chapter-list"]')
                endpoint = _same_source(link.get('hx-get', ''), url) if link else ''
                if endpoint and urlsplit(endpoint).path == urlsplit(url).path.rsplit('/', 1)[0] + '/full-chapter-list':
                    html = _fetch(endpoint)
                    value = parse_chapter_count(html, url, source, full_list=True)
                else:
                    value = parse_chapter_count(html, url, source)
            else:
                value = parse_chapter_count(html, url, source)
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        value = dict(_UNKNOWN)
    with _LOCK:
        if len(_CACHE) > 1000:
            _CACHE.clear()
        _CACHE[url] = (time.monotonic(), value)
    return dict(value)


def enrich_chapter_counts(results: list[dict], progress=None) -> list[dict]:
    if not results:
        return []
    enriched = [dict(row) for row in results]
    # Round-robin sources avoids filling the queue with one host's entire result set.
    groups = {}
    for index, row in enumerate(results):
        groups.setdefault(row.get('source', ''), []).append(index)
    order = []
    while any(groups.values()):
        for indices in groups.values():
            if indices:
                order.append(indices.pop(0))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(get_chapter_count, results[index]): index for index in order}
        for done, future in enumerate(as_completed(futures), 1):
            enriched[futures[future]].update(future.result())
            if progress:
                progress(done, len(results))
    return enriched


def chapter_count_label(result: dict) -> str:
    count = result.get('chapter_count')
    if count is None:
        return 'Chapter count unavailable'
    if result.get('chapter_count_kind') == 'partial':
        return f'At least {count:,} chapters'
    return f'{count:,} chapters'


def result_rank(result: dict, query: str) -> tuple:
    from panelyra.services.title_search import normalize_title
    title = normalize_title(result['title'])
    wanted = normalize_title(query)
    title_words = title.split()
    query_words = wanted.split()
    exact_tokens = sum(word in title_words for word in query_words)
    prefix_tokens = sum(any(candidate.startswith(word) for candidate in title_words) for word in query_words)
    if title == wanted:
        match_class = 0
    elif title.startswith(wanted + ' ') or title.startswith(wanted):
        match_class = 1
    elif f' {wanted} ' in f' {title} ':
        match_class = 2
    elif query_words and exact_tokens == len(query_words):
        match_class = 3
    elif query_words and prefix_tokens == len(query_words):
        match_class = 4
    else:
        match_class = 5
    similarity = SequenceMatcher(None, wanted, title).ratio()
    count = result.get('chapter_count')
    # Title relevance is authoritative. Chapter count only orders results with a
    # comparable title match, so a popular but unrelated series cannot jump ahead.
    return (
        match_class,
        -exact_tokens,
        -prefix_tokens,
        -similarity,
        abs(len(title) - len(wanted)),
        -(count if count is not None else -1),
        title,
        result['source'],
    )
