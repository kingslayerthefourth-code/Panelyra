"""Strict extraction of explicit MangaReader page lists, without executing scripts."""
import json
import re
from urllib.parse import urljoin, urlsplit


def mangareader_pages(soup, chapter_url):
    scripts = [script.get_text() for script in soup.find_all("script")
               if re.search(r"\bts_reader\s*\.\s*run\s*\(", script.get_text())]
    if not scripts:
        return None
    if len(scripts) != 1:
        raise ValueError("Multiple reader configurations need review.")
    raw = re.split(r"\bts_reader\s*\.\s*run\s*\(", scripts[0], maxsplit=1)[1].lstrip()
    try:
        config = json.JSONDecoder().raw_decode(raw)[0]
        sources = config["sources"]
        images = sources[0]["images"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("Reader page data is missing or malformed.") from exc
    if not isinstance(images, list) or not images or len(images) > 2000:
        raise ValueError("Reader returned an empty or oversized page list.")
    pages = []
    for image in images:
        if not isinstance(image, str) or not image.strip():
            raise ValueError("Reader contains an invalid page URL.")
        url = urljoin(chapter_url, image.strip())
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Reader contains an unsafe page URL.")
        if url not in pages:
            pages.append(url)
    return pages
