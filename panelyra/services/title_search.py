"""Read-only, bounded title discovery. Never invokes a browser or downloader."""
from __future__ import annotations

import json
import ipaddress
import re
from html import escape
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

# Each adapter matches a verified public search page, not a guessed URL.
SOURCES = {
    "Asura": "https://asurascans.com",
    "Thunder": "https://en-thunderscans.com",
    "Kayn": "https://kaynscans.com",
    "MangaPill": "https://mangapill.com",
    "WeebCentral": "https://weebcentral.com",
    "MangaRead": "https://www.mangaread.org",
    "MangaSushi": "https://mangasushi.org",
    "LHTranslation": "https://lhtranslation.net",
    "ManhuaPlus": "https://manhuaplus.com",
    "Rolia": "https://roliascan.com",
    "GalaxyDegen": "https://gdscans.com",
    "MangaTaro": "https://mangataro.org",
    "Drake": "https://drakecomic.net",
    "Tritinia": "https://tritinia.org",
    'Athrea': 'https://athreascans.com',
    'Eva': 'https://evascans.net',
    'Lagoon': 'https://lagoonscans.com',
    'Madara': 'https://madarascans.net',
    'Raven': 'https://ravenscans.org',
    'Rokari': 'https://rokaricomics.com',
    'Scythe': 'https://scythescans.com',
    'Yaksha Comics': 'https://yakshacomics.com',
    'Violet': 'https://violetmanga.com',
    'Mist': 'https://mistscans.com',
    'Rithar': 'https://ritharscans.com',
    'MangaKatana': 'https://mangakatana.com',
    "MangaDex": "https://api.mangadex.org",
}

GROUP_MAPPING_SOURCE = "https://github.com/GooglyBlox/comick-group-mapping"
_NON_SEARCH_HOSTS = (
    "mangadex.org", "comick.io", "twitter.com", "x.com", "facebook.com", "discord.",
    "web.archive.org", "mangaupdates.com", "reddit.com", "youtube.com", "t.me",
    "tumblr.com", "blogspot.com", "wordpress.com", "carrd.co", "sites.google.com",
)
_OFFICIAL_PLATFORMS = {
    "bilibili comics", "inkr comics", "k manga", "kodansha usa", "manga up!",
    "mangaplus", "namicomi", "tapas", "tappytoon", "toomics", "viz media", "webtoon",
}
_FEATURED_COMMUNITY_GROUPS = {
    "AbyssToons", "AGR Comics", "Animated Glitched Scans", "Arang Scans", "ArcaneScans.com",
    "ARENA_SCANS", "ARVEN", "Asmodeus Scans", "Astra Scans", "Cosmic Scans",
    "Death Toll Scans", "Dragon Tea", "Elf Toon", "Ezmanga", "FireScans", "Flame Comics",
    "FlixScans", "Freak Scans", "Genz Toons", "Hivetoons", "Imperfect Comic",
    "Infernal Void Scans", "LeviatanScans", "Lua Comic", "LynX Scans", "Magusmanga",
    "Manhuaga", "ManhwaFreak", "Necro_Scans", "Nightscans", "OmegaScans", "Philia Scans",
    "QiScans", "Radiant", "Realm Scans", "Reaper_Scans", "RESET SCANS", "Rizz Fables",
    "Scylla Scans", "Setsu Scans", "Spider Scans", "StoneScape", "UTOON", "Vortex Scans",
    "Witch Scans",
}


def _load_community_sources() -> dict[str, str]:
    """Load independently hosted HTTPS groups from the bundled public mapping snapshot."""
    mapping = Path(__file__).resolve().parents[1] / "data" / "comick_groups.json"
    try:
        records = json.loads(mapping.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    existing_names = {name.casefold() for name in SOURCES}
    existing_hosts = {urlsplit(url).hostname for url in SOURCES.values()}
    candidates: list[tuple[str, str, str]] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        title = " ".join(str(record.get("title", "")).split())
        parsed = urlsplit(str(record.get("url", "")).strip())
        host = (parsed.hostname or "").lower()
        if (
            not title
            or title not in _FEATURED_COMMUNITY_GROUPS
            or title.casefold() in existing_names
            or title.casefold() in _OFFICIAL_PLATFORMS
            or parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/", "/home-hi/"}
            or any(blocked in host for blocked in _NON_SEARCH_HOSTS)
        ):
            continue
        candidates.append((title, f"https://{parsed.netloc}", host))
    community: dict[str, str] = {}
    for title, origin, host in sorted(candidates, key=lambda item: item[0].casefold()):
        if host in existing_hosts:
            continue
        existing_hosts.add(host)
        community[title] = origin
    return community


COMMUNITY_SOURCES = _load_community_sources()
SOURCES.update(COMMUNITY_SOURCES)
# Search every verified regular and curated community source by default. MangaDex's
# API is currently unreachable on this connection, so it remains available but opt-in.
DEFAULT_SOURCES = [name for name in SOURCES if name != "MangaDex"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36", "Accept": "text/html,application/json"}
EMPTY = "No matching titles found."
RESULTS_PER_PAGE = 12
SOURCE_SEARCH_BUDGET_SECONDS = 18
_SOURCE_POOL = ThreadPoolExecutor(max_workers=24, thread_name_prefix="panelyra-title-source")


def _cover_placeholder(st, *, compact: bool = False) -> None:
    """Keep result cards aligned when a source does not publish cover art."""
    height = 112 if compact else 150
    st.markdown(
        f'<div aria-label="Cover unavailable" style="height:{height}px;border-radius:10px;'
        'display:grid;place-items:center;background:linear-gradient(145deg,#252833,#171920);'
        'border:1px solid rgba(255,255,255,.08);color:#8d93a3;font-size:.72rem">'
        '<span>Cover unavailable</span></div>',
        unsafe_allow_html=True,
    )


def _inject_download_style(st):
    st.markdown("""<style>
    .st-key-manga_result_grid [data-testid="stImage"] img,
    .st-key-manga_selected_title [data-testid="stImage"] img,
    .st-key-manga_download_workspace [data-testid="stImage"] img {
        width:100%;height:144px;object-fit:cover;border-radius:10px;
    }
    .st-key-manga_result_grid [data-testid="stHorizontalBlock"] {align-items:stretch;}
    .st-key-manga_result_grid [data-testid="stVerticalBlockBorderWrapper"] {height:100%;}
    .st-key-manga_pagination button {min-width:36px;min-height:44px;padding:0 .55rem;}
    .st-key-manga_pagination [data-testid="stBaseButton-primary"]:disabled {opacity:1;}
    .st-key-manga_pagination [data-testid="stHorizontalBlock"] {gap:.3rem;flex-wrap:wrap;}
    .st-key-manga_download_workspace button {transition:background-color .16s ease,border-color .16s ease;}
    .st-key-manga_download_workspace button:focus-visible {outline:2px solid #54d4c5;outline-offset:3px;}
    @media(max-width:760px) {
        .st-key-manga_result_grid > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {flex-wrap:wrap;}
        .st-key-manga_result_grid > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:100%!important;flex:1 1 100%!important;}
        [class*="st-key-manga_title_card_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {flex-wrap:nowrap!important;}
        [class*="st-key-manga_title_card_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:0!important;}
        [class*="st-key-manga_title_card_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {flex:0 0 96px!important;width:96px!important;}
        [class*="st-key-manga_title_card_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child {flex:1 1 0!important;}
    }
    @media(prefers-reduced-motion:reduce) {
        .st-key-manga_download_workspace button {transition:none;}
    }
    </style>""", unsafe_allow_html=True)


def render_cover(st, result: dict, *, compact: bool = False) -> None:
    """Render a verified search-result cover or a stable, accessible fallback."""
    cover = str(result.get("cover", "")).strip()
    if cover:
        st.image(cover, width="stretch")
    else:
        _cover_placeholder(st, compact=compact)


def _pagination_items(page: int, page_count: int) -> list[int | None]:
    """Return familiar first/nearby/last page controls with ellipsis gaps."""
    if page_count <= 7:
        return list(range(1, page_count + 1))
    if page <= 4:
        return [1, 2, 3, 4, 5, None, page_count]
    if page >= page_count - 3:
        return [1, None, *range(page_count - 4, page_count + 1)]
    return [1, None, page - 1, page, page + 1, None, page_count]


def _render_pagination(st, page: int, page_count: int, *, position: str) -> None:
    if page_count <= 1:
        return
    items = _pagination_items(page, page_count)
    with st.container(horizontal=True, horizontal_alignment="center", gap="small", key="manga_pagination"):
        if st.button(
            "‹", key=f"manga_page_previous_{position}", disabled=page <= 1,
            help="Previous page", width="content",
        ):
            st.session_state.manga_results_page = page - 1
            st.rerun()
        for item in items:
            if item is None:
                st.markdown("…")
            elif st.button(
                str(item), key=f"manga_page_{position}_{item}", disabled=item == page,
                type="primary" if item == page else "secondary", width="content",
                help=f"Page {item}" + (" (current page)" if item == page else ""),
            ):
                st.session_state.manga_results_page = item
                st.rerun()
        if st.button(
            "›", key=f"manga_page_next_{position}", disabled=page >= page_count,
            help="Next page", width="content",
        ):
            st.session_state.manga_results_page = page + 1
            st.rerun()


def search_request(source: str, query: str) -> tuple[str, dict]:
    base = SOURCES[source]
    if source == "MangaDex":
        return base + "/manga", {"title": query, "limit": 20, "includes[]": "cover_art", "contentRating[]": ["safe", "suggestive"]}
    if source == "Asura":
        return base + "/browse", {"q": query}
    if source in {"Kayn", "Drake"}:
        return base + "/series", {"q": query, "type": "Manhwa,Manhua,Manga,Webtoon"}
    if source in {"Mist", "Writers"}:
        return base + "/series/", {"q": query}
    if source == "Rithar":
        return base + "/search", {"title": query}
    if source == "MangaKatana":
        return base + "/", {"search": query}
    if source == "MangaPill":
        return base + "/search", {"q": query}
    if source == "WeebCentral":
        return base + "/search/data", {"text": query, "display_mode": "Full Display"}
    params = {"s": query}
    if source in {"MangaRead", "MangaSushi", "LHTranslation", "ManhuaPlus", "GalaxyDegen", "Tritinia", "KS Group", "MangaYY", "Yaksha Comics"}:
        params["post_type"] = "wp-manga"
    return base + "/", params


def normalize_title(text: str) -> str:
    import unicodedata
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def series_path(source: str, path: str) -> bool:
    if source in {"Kayn", "Drake"}:
        return bool(re.fullmatch(r"/series/comic/[^/]+/?", path))
    if source == "MangaPill":
        return bool(re.fullmatch(r"/manga/\d+/[^/]+/?", path))
    if source == "WeebCentral":
        return bool(re.fullmatch(r"/series/[A-Z0-9]{26}/[^/]+/?", path))
    return bool(re.fullmatch(r"/(?:manga|series|comics?|webtoon|manhwa|title|project)/[^/]+/?", path))


def safe_url(value: str, base: str) -> str:
    url = urlsplit(urljoin(base, value))
    if url.scheme != "https" or url.hostname != urlsplit(base).hostname or url.username or url.password:
        return ""
    return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))


def safe_cover_url(value: str, base: str) -> str:
    """Series links stay on their source; cover images may use a public CDN."""
    url = urlsplit(urljoin(base, str(value).strip()))
    host = (url.hostname or "").casefold()
    if url.scheme != "https" or not host or url.username or url.password:
        return ""
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))


def parse_madara(html: str, base: str, source: str, query: str) -> list[dict]:
    """Parse supported series links; reject chapter links and unrelated hosts."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    if any(marker in text for marker in ("verify you are human", "just a moment", "checking your browser", "sign in to continue", "log in to continue", "access denied", "ip address has been temporarily blocked")):
        raise ValueError("Source requires browser verification or login; skipped.")
    tokens = normalize_title(query).split()
    results, priorities = {}, {}
    recognized = 0
    for anchor in soup.select('a[href]'):
        url = safe_url(anchor.get("href", ""), base)
        path = urlsplit(url).path
        if not url or not series_path(source, path):
            continue
        recognized += 1
        img = anchor.find("img")
        heading = anchor.select_one("h2, h3, h4, .series-title, .post-title, .tt, .font-black")
        if heading:
            title, priority = heading.get_text(" ", strip=True), 4
        elif anchor.get("title"):
            title, priority = anchor["title"], 3
        elif img and img.get("alt"):
            title, priority = re.sub(r" cover$", "", img["alt"], flags=re.I), 1
        else:
            title, priority = anchor.get_text(" ", strip=True), 2
        title = " ".join(str(title).split())[:240]
        normalized = normalize_title(title)
        if not title or not all(token in normalized for token in tokens):
            continue
        # Remove marketing ref parameters; chapter URLs are never accepted.
        key = urlunsplit((urlsplit(url).scheme, urlsplit(url).netloc, path.rstrip("/"), "", ""))
        cover = ""
        if img:
            candidates = [img.get("data-src"), img.get("data-lazy-src"), img.get("src")]
            candidates.extend(item.strip().split(" ")[0] for item in str(img.get("data-srcset") or img.get("srcset") or "").split(","))
            cover = next((value for candidate in candidates if candidate and (value := safe_cover_url(candidate, base))), "")
        if key not in results or priority > priorities[key]:
            results[key] = {"title": title, "url": key, "source": source, "cover": cover or results.get(key, {}).get("cover", "")}
            priorities[key] = priority
        elif cover:
            results[key]["cover"] = cover
    known_empty = any(marker in text for marker in ("no results", "no matching", "no matches found", "no comics match", "no series match", "nothing found", "no manga found", "no posts found", "not found any", "no series found", "0 results", "no comics found", "no search results"))
    known_empty = known_empty or bool(soup.title and "search" in soup.title.get_text().lower() and "not found" in text)
    if source == "Madara" and "search" in text and any(h.get_text(" ", strip=True).casefold() == "not found" for h in soup.select("h1,h2,h3")):
        known_empty = True
    if not results and not known_empty and not recognized:
        raise ValueError("Search layout was not recognized; results could not be verified. Use Paste link.")
    return sorted(results.values(), key=lambda item: (normalize_title(item["title"]) != normalize_title(query), len(item["title"])))[:20]


def parse_mangadex(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Source returned an invalid search response.")
    results = []
    for entry in payload["data"]:
        identity = str(entry.get("id", ""))
        if not re.fullmatch(r"[a-f0-9-]{36}", identity):
            continue
        attrs = entry.get("attributes", {})
        if attrs.get("contentRating") in {"erotica", "pornographic"}:
            continue
        titles = attrs.get("title", {})
        title = titles.get("en") or next(iter(titles.values()), "Untitled")
        cover = ""
        for relation in entry.get("relationships", []):
            filename = relation.get("attributes", {}).get("fileName", "")
            if relation.get("type") == "cover_art" and re.fullmatch(r"[\w.-]+", filename):
                cover = f"https://uploads.mangadex.org/covers/{identity}/{filename}.256.jpg"
                break
        results.append({"title": str(title)[:240], "url": f"https://mangadex.org/title/{identity}", "source": "MangaDex", "cover": cover})
    return results[:20]


def search_source(source: str, query: str) -> tuple[list[dict], str]:
    if source not in SOURCES:
        return [], "Unknown search source."
    base = SOURCES[source]
    try:
        url, params = search_request(source, query)
        # Follow only same-source HTTPS redirects (for canonical trailing slashes).
        for redirect in range(3):
            with requests.get(url, params=params, headers=HEADERS, timeout=(3, 7), stream=True, allow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    target = safe_url(response.headers.get("Location", ""), base)
                    if not target or redirect == 2:
                        raise ValueError("Source moved to another host or redirected too often; adapter needs review.")
                    url, params = target, None
                    continue
                if response.status_code in {401, 403}:
                    raise ValueError("Source requires browser verification or login; use its website or Capture.")
                if response.status_code == 429:
                    raise ValueError("Source is rate-limiting searches. Please retry later.")
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > 3_000_000:
                        raise ValueError("Source returned an oversized search response.")
                if source == "MangaDex":
                    import json
                    results = parse_mangadex(json.loads(data))
                else:
                    results = parse_madara(data.decode("utf-8", errors="replace"), base, source, query)
                return results, "" if results else EMPTY

    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
        return [], str(exc) if isinstance(exc, ValueError) else "Source unavailable or returned an invalid response. Retry or use Paste link."


def search_titles(query: str, sources: list[str], *, progress=None, on_results=None,
                  include_counts=True, background=False, cancelled=None) -> tuple[list[dict], dict[str, str]]:
    query = " ".join(query.split())
    if not 2 <= len(query) <= 120:
        raise ValueError("Enter between 2 and 120 characters of the title.")
    selected = [name for name in SOURCES if name in sources]
    if not selected:
        raise ValueError("Choose at least one search source.")
    responses_by_source = {}
    # Every request goes to a different source host. A wider bounded pool keeps a
    # few offline sites from making the entire all-sources search appear frozen.
    futures = {_SOURCE_POOL.submit(search_source, name, query): name for name in selected}
    try:
        for done, future in enumerate(as_completed(futures, timeout=None if background else SOURCE_SEARCH_BUDGET_SECONDS), 1):
            if cancelled and cancelled():
                break
            try:
                responses_by_source[futures[future]] = future.result()
            except Exception:
                responses_by_source[futures[future]] = ([], "Source check could not finish.")
            if progress:
                progress(done, len(selected))
            if on_results:
                rows = [row for name in selected for row in responses_by_source.get(name, ([], ""))[0]]
                notices = {name: value[1] for name, value in responses_by_source.items() if value[1]}
                on_results(rows, notices, done, len(selected))
    except FuturesTimeoutError:
        # Slow, offline, or protected sites must not block usable matches from
        # healthy sources. Already-running checks finish quietly in the worker
        # pool; queued work is cancelled when the visible search budget expires.
        pass
    finally:
        for future in futures:
            future.cancel()
    for future, name in futures.items():
        if name in responses_by_source:
            continue
        if future.done() and not future.cancelled():
            try:
                responses_by_source[name] = future.result()
                continue
            except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
                pass
        responses_by_source[name] = ([], "Source check exceeded the foreground search budget.")
    if progress:
        progress(len(selected), len(selected))
    results, notices = [], {}
    for name in selected:
        matches, notice = responses_by_source[name]
        results.extend(matches)
        if notice:
            notices[name] = notice
    if not include_counts:
        return results, notices
    from panelyra.services.chapter_counts import enrich_chapter_counts
    if progress:
        source_total = len(selected)
        count_progress = lambda done, total: progress(source_total + done, source_total + total)
    else:
        count_progress = None
    return enrich_chapter_counts(results, count_progress), notices


def web_search_url(query: str) -> str:
    return "https://www.google.com/search?" + urlencode({"q": query + " manhwa series"})


def render_title_search() -> None:
    import streamlit as st
    from panelyra.services.title_discovery import TitleSearchTask
    _inject_download_style(st)
    # Upgrade existing open sessions to the requested all-source default once.
    if st.session_state.get("manga_sources_revision") != 2:
        st.session_state.manga_search_sources = list(DEFAULT_SOURCES)
        st.session_state.manga_sources_revision = 2

    def queue_search() -> None:
        st.session_state.pop("title_search_error", None)
        query = " ".join(str(st.session_state.get("manga_search_query", "")).split())
        sources = list(st.session_state.get("manga_search_sources", DEFAULT_SOURCES))
        if not 2 <= len(query) <= 120:
            st.session_state.title_search_error = "Enter at least two letters of the title."
            return
        if not sources:
            st.session_state.title_search_error = "Select at least one search source."
            return
        previous = st.session_state.get("manga_title_task")
        if previous and previous.matches(query, sources):
            return
        if previous:
            previous.cancel()
        st.session_state.pop("title_search_result", None)
        st.session_state.pop("manga_result_source", None)
        st.session_state.manga_results_page = 1
        st.session_state.manga_result_ids = {}
        st.session_state.manga_title_task = TitleSearchTask(query, sources, search_titles)
        st.session_state.manga_title_task.wait()

    with st.form("manhwa_title_search"):
        field, submit = st.columns([5, 1], vertical_alignment="bottom")
        with field:
            st.text_input("Search by title", placeholder="Enter a manga or manhwa title", key="manga_search_query")
        with submit:
            st.form_submit_button("Search", type="primary", width="stretch", on_click=queue_search)
        with st.expander("Search sources"):
            sources = st.multiselect("Sources", list(SOURCES), default=DEFAULT_SOURCES, key="manga_search_sources")
            st.caption(f"{len(sources)} sources selected. Search checks them automatically.")
    if st.session_state.get("title_search_error"):
        st.error(st.session_state.pop("title_search_error"))
    task = st.session_state.get("manga_title_task")
    snapshot = task.snapshot() if task else None
    busy = snapshot is not None and (not snapshot.finished or snapshot.details_pending)

    @st.fragment(run_every=0.8 if busy else None)
    def results_panel():
        current = task.snapshot() if task else None
        if current:
            st.session_state.title_search_result = (task.query, current.results, current.notices)
            from panelyra.services.waiting_feedback import duration
            elapsed = monotonic() - task.created_at
            if not current.finished:
                st.progress(current.checked / max(1, current.total), text=f"{len(current.results)} titles found · {current.checked}/{current.total} sources checked")
                st.caption(f"Finding matches · {duration(elapsed)} elapsed · results stay available while the remaining sources respond.")
            else:
                st.progress(1.0, text=f"Search complete · {len(current.results)} titles found · {current.checked}/{current.total} sources checked")
            if not current.finished and not current.results:
                st.caption("Titles will appear here as they arrive.")
                return
            elif current.error and not current.results:
                st.info(current.error)
                return
        foreground = bool(current and not current.finished and monotonic() - task.created_at < SOURCE_SEARCH_BUDGET_SECONDS)
        _render_search_results(st, task, searching=foreground)
        # The fragment may keep polling an already-complete snapshot. Avoid a
        # whole-page rerun just to stop its timer: it dims the active workspace.

    results_panel()


def _render_search_results(st, task=None, *, searching=False):
    saved = st.session_state.get("title_search_result")
    if not saved:
        st.caption("Find a title across your sources, or paste a series link.")
        return
    query, results, notices = saved
    if not results:
        st.info("No matches for this title. Try a shorter title or paste a series link.")
        st.link_button("Search the web", web_search_url(query))
        return
    from panelyra.services.chapter_counts import chapter_count_label, result_rank
    results = sorted(results, key=lambda item: result_rank(item, query))
    st.caption(f"{len(results)} titles for “{query}” · closest matches first" + (" · finding more…" if searching else ""))
    if "manga_result_source" not in st.session_state:
        st.session_state.manga_result_source = "All sources"
    def reset_results_page() -> None:
        st.session_state.manga_results_page = 1

    options = ["All sources"] + sorted({item["source"] for item in results})
    if st.session_state.manga_result_source not in options:
        st.session_state.manga_result_source = "All sources"
    filter_source = st.selectbox(
        "Show results from",
        options,
        key="manga_result_source",
        on_change=reset_results_page,
    )
    visible = [(index, item) for index, item in enumerate(results) if filter_source == "All sources" or item["source"] == filter_source]
    page_count = max(1, (len(visible) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    try:
        page = int(st.session_state.get("manga_results_page", 1))
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), page_count)
    st.session_state.manga_results_page = page
    start = (page - 1) * RESULTS_PER_PAGE
    end = min(start + RESULTS_PER_PAGE, len(visible))
    page_rows = visible[start:end]
    if task:
        task.request_details([row for _, row in page_rows])
    ids = st.session_state.setdefault("manga_result_ids", {})
    with st.container(key="manga_result_grid"):
        for offset in range(0, len(page_rows), 2):
            columns = st.columns(2)
            for column, (_, result) in zip(columns, page_rows[offset:offset + 2]):
                identity = ids.setdefault(result["url"], len(ids))
                with column.container(border=True, key=f"manga_title_card_{identity}"):
                    cover_column, details = st.columns([1, 2.15], gap="small", vertical_alignment="center")
                    with cover_column:
                        render_cover(st, result, compact=True)
                    with details:
                        st.markdown(f"**{escape(str(result['title']))}**", unsafe_allow_html=True)
                        count = chapter_count_label(result) if result.get("chapter_count") is not None else "Checking chapters…" if "chapter_count" not in result else ""
                        st.caption(result["source"] + (" · " + count if count else ""))
                        if st.button("Choose title", key=f"manga_choose_{identity}", width="stretch"):
                            st.session_state.manga_selected_series = dict(result)
                            st.session_state.manga_series_url = result["url"]
                            if st.session_state.get("manga_chapter_series") != result["url"]:
                                st.session_state.pop("manga_chapters", None)
                                st.session_state.manga_chapter_series = result["url"]
                            st.rerun(scope="app")
    st.caption(f"Showing {start + 1}–{end} of {len(visible)} titles")
    _render_pagination(st, page, page_count, position="bottom")
