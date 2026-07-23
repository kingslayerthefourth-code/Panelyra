"""Shared reader detection and navigation for authorized content workflows."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selenium.common.exceptions import JavascriptException, WebDriverException


READER_LAYOUTS = ("auto", "continuous", "next-chapter", "paginated", "mixed")

DEFAULT_NEXT_PAGE_SELECTOR = (
    "a.next-page, button.next-page, [data-testid*='next-page' i], "
    "a[aria-label*='next page' i], button[aria-label*='next page' i], "
    "a[title*='next page' i], button[title*='next page' i], "
    ".pagination-next a, .pagination-next button, .reader-next-page"
)
DEFAULT_PAGE_COUNTER_SELECTOR = (
    "[data-testid*='page-count' i], [aria-label*='page' i], "
    "[role='combobox'], .page-counter, .reader-page, .pagination"
)
DEFAULT_NEXT_CHAPTER_SELECTOR = (
    "a.next-chapter, button.next-chapter, [data-testid*='next-chapter' i], "
    "a.next-episode, button.next-episode, [data-testid*='next-episode' i], "
    "a[aria-label*='next chapter' i], button[aria-label*='next chapter' i], "
    "a[aria-label*='next episode' i], button[aria-label*='next episode' i], "
    "a[title*='next chapter' i], button[title*='next chapter' i], "
    "a[title*='next episode' i], button[title*='next episode' i], "
    "[data-action*='next-chapter' i], [data-action*='next-episode' i], "
    "a[rel='next']"
)
DEFAULT_CHAPTER_LIST_SELECTOR = (
    "a[href*='chapter' i], a[href*='episode' i], "
    "a[href*='/chap-' i], a[href*='/ch-' i], a[href*='/ep-' i], "
    "option[value*='chapter' i], option[value*='episode' i], "
    ".chapter-list a, .episode-list a, [class*='chapter' i] a"
)


@dataclass(frozen=True)
class ReaderSelectors:
    next_chapter: str
    next_page: str
    page_counter: str
    comment_editor: str
    chapter_list: str = DEFAULT_CHAPTER_LIST_SELECTOR


@dataclass(frozen=True)
class ReaderSnapshot:
    url: str
    title: str
    page_current: int
    page_total: int
    image_signature: str
    image_count: int
    visible_comment_editors: int
    has_next_page: bool
    has_next_chapter: bool
    scroll_height: int
    viewport_height: int


def parse_page_counter_text(value: str) -> tuple[int, int]:
    match = re.search(r"(?<!\d)(\d{1,4})\s*(?:/|of)\s*(\d{1,4})(?!\d)", value, re.IGNORECASE)
    if not match:
        return 0, 0
    current, total = int(match.group(1)), int(match.group(2))
    if current < 1 or total < current:
        return 0, 0
    return current, total


def canonical_chapter_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"page", "p", "reader_page", "scroll", "comment"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urlencode(query), ""))


def reader_snapshot(driver: object, selectors: ReaderSelectors) -> ReaderSnapshot:
    script = """
        const nextChapterSelector = arguments[0];
        const nextPageSelector = arguments[1];
        const pageCounterSelector = arguments[2];
        const commentSelector = arguments[3];
        const safeAll = selector => {
            if (!selector) return [];
            try { return Array.from(document.querySelectorAll(selector)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
                && style.display !== "none" && style.visibility !== "hidden"
                && !node.disabled && node.getAttribute("aria-disabled") !== "true";
        };
        const text = node => [
            node.innerText || "", node.textContent || "",
            node.getAttribute("aria-label") || "", node.getAttribute("title") || ""
        ].join(" ").replace(/\\s+/g, " ").trim();
        let current = 0, total = 0;
        const counters = [
            ...safeAll(pageCounterSelector),
            ...safeAll("[role='combobox'], button, [aria-label*='page' i]")
        ];
        for (const node of counters) {
            if (!visible(node)) continue;
            const match = text(node).match(/(?:^|\\s)(\\d{1,4})\\s*(?:\\/|of)\\s*(\\d{1,4})(?:\\s|$)/i);
            if (!match) continue;
            const candidateCurrent = Number(match[1]), candidateTotal = Number(match[2]);
            if (candidateCurrent >= 1 && candidateTotal >= candidateCurrent && candidateTotal > total) {
                current = candidateCurrent;
                total = candidateTotal;
            }
        }
        const imageSources = Array.from(document.images).map(image =>
            image.currentSrc || image.src || image.getAttribute("data-src") || ""
        ).filter(Boolean);
        const semanticNodes = safeAll("a,button,[role='button']");
        const semanticNextPage = semanticNodes.some(node =>
            visible(node) && /\\bnext\\s+page\\b|\\bpage\\s+next\\b/i.test(text(node))
        );
        const semanticNextChapter = semanticNodes.some(node =>
            visible(node) && /\\bnext\\s+(chapter|episode)\\b|\\b(chapter|episode)\\s+next\\b/i.test(text(node))
        );
        return {
            url: location.href,
            title: document.title || "",
            pageCurrent: current,
            pageTotal: total,
            imageSignature: imageSources.slice(0, 40).join("|"),
            imageCount: imageSources.length,
            visibleCommentEditors: safeAll(commentSelector).filter(visible).length,
            hasNextPage: safeAll(nextPageSelector).some(visible) || semanticNextPage,
            hasNextChapter: safeAll(nextChapterSelector).some(visible) || semanticNextChapter,
            scrollHeight: Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0),
            viewportHeight: window.innerHeight || 0
        };
    """
    try:
        raw = driver.execute_script(
            script,
            selectors.next_chapter,
            selectors.next_page,
            selectors.page_counter,
            selectors.comment_editor,
        ) or {}
    except (JavascriptException, WebDriverException):
        raw = {}
    return ReaderSnapshot(
        url=str(raw.get("url", getattr(driver, "current_url", ""))),
        title=str(raw.get("title", "")),
        page_current=int(raw.get("pageCurrent", 0) or 0),
        page_total=int(raw.get("pageTotal", 0) or 0),
        image_signature=str(raw.get("imageSignature", "")),
        image_count=int(raw.get("imageCount", 0) or 0),
        visible_comment_editors=int(raw.get("visibleCommentEditors", 0) or 0),
        has_next_page=bool(raw.get("hasNextPage", False)),
        has_next_chapter=bool(raw.get("hasNextChapter", False)),
        scroll_height=int(raw.get("scrollHeight", 0) or 0),
        viewport_height=int(raw.get("viewportHeight", 0) or 0),
    )


def classify_reader_layout(snapshot: ReaderSnapshot, preferred: str = "auto") -> str:
    if preferred in READER_LAYOUTS and preferred != "auto":
        return preferred
    paginated = snapshot.page_total > 1 or snapshot.has_next_page
    if snapshot.visible_comment_editors > 1:
        return "continuous"
    if paginated and snapshot.has_next_chapter:
        return "mixed"
    if paginated:
        return "paginated"
    if snapshot.has_next_chapter:
        return "next-chapter"
    if snapshot.scroll_height > max(1, snapshot.viewport_height) * 2:
        return "continuous"
    return "next-chapter"


def reader_changed(before: ReaderSnapshot, after: ReaderSnapshot) -> bool:
    return any(
        (
            after.url != before.url,
            after.title != before.title,
            after.page_current != before.page_current,
            after.image_signature != before.image_signature,
            after.image_count != before.image_count,
        )
    )


def wait_for_reader_change(
    driver: object,
    before: ReaderSnapshot,
    selectors: ReaderSelectors,
    timeout_seconds: float = 12.0,
) -> Optional[ReaderSnapshot]:
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while time.monotonic() < deadline:
        time.sleep(0.2)
        after = reader_snapshot(driver, selectors)
        if reader_changed(before, after):
            return after
    return None


def _click_control(
    driver: object,
    selector: str,
    *,
    control_kind: str,
    before: ReaderSnapshot,
) -> bool:
    script = """
        const selector = arguments[0], kind = arguments[1], pageCurrent = arguments[2], pageTotal = arguments[3];
        const safeAll = value => {
            if (!value) return [];
            try { return Array.from(document.querySelectorAll(value)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
                && style.display !== "none" && style.visibility !== "hidden"
                && !node.disabled && node.getAttribute("aria-disabled") !== "true";
        };
        const label = node => [
            node.innerText || "", node.textContent || "", node.getAttribute("aria-label") || "",
            node.getAttribute("title") || "", node.getAttribute("rel") || "",
            node.id || "", String(node.className || "")
        ].join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
        const semantic = safeAll("a,button,[role='button']");
        let candidates = [...safeAll(selector), ...semantic].filter(visible);
        candidates = Array.from(new Set(candidates)).filter(node => {
            const text = label(node);
            if (kind === "page") {
                if (/prev|previous|back/.test(text)) return false;
                if (/next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(text)) return false;
                return /next\\s+page|page\\s+next|pagination-next|reader-next-page/.test(text)
                    || safeAll(selector).includes(node);
            }
            if (/prev|previous|back/.test(text)) return false;
            return /next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(text)
                || safeAll(selector).includes(node);
        });
        if (kind === "page" && pageTotal > 0 && pageCurrent >= pageTotal) return false;
        if (!candidates.length && kind === "page") {
            const counters = semantic.filter(node =>
                /^\\s*\\d+\\s*(?:\\/|of)\\s*\\d+\\s*$/i.test((node.innerText || node.textContent || "").trim())
            );
            if (counters.length) {
                counters.sort((a, b) =>
                    (b.getBoundingClientRect().top + scrollY) - (a.getBoundingClientRect().top + scrollY)
                );
                const rect = counters[0].getBoundingClientRect();
                candidates = semantic.filter(node => {
                    const candidate = node.getBoundingClientRect();
                    return candidate.left > rect.left + 8 && Math.abs(candidate.top - rect.top) < 72;
                });
                candidates.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
            }
        }
        if (!candidates.length) return false;
        candidates.sort((a, b) => {
            const aText = label(a), bText = label(b);
            const aScore = kind === "chapter"
                ? (/next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(aText) ? 100 : 0)
                : (/next\\s+page|page\\s+next/.test(aText) ? 100 : 0);
            const bScore = kind === "chapter"
                ? (/next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(bText) ? 100 : 0)
                : (/next\\s+page|page\\s+next/.test(bText) ? 100 : 0);
            return bScore - aScore;
        });
        const target = candidates[0];
        target.scrollIntoView({block: "center", inline: "center"});
        target.click();
        return true;
    """
    try:
        return bool(
            driver.execute_script(
                script,
                selector,
                control_kind,
                before.page_current,
                before.page_total,
            )
        )
    except (JavascriptException, WebDriverException):
        return False


def advance_reader_page(
    driver: object,
    selectors: ReaderSelectors,
    timeout_seconds: float = 12.0,
) -> Optional[ReaderSnapshot]:
    before = reader_snapshot(driver, selectors)
    if before.page_total > 0 and before.page_current >= before.page_total:
        return None
    if not _click_control(driver, selectors.next_page, control_kind="page", before=before):
        return None
    return wait_for_reader_change(driver, before, selectors, timeout_seconds)


def next_chapter_href(driver: object, selectors: ReaderSelectors) -> str:
    script = """
        const selector = arguments[0];
        const safeAll = value => {
            if (!value) return [];
            try { return Array.from(document.querySelectorAll(value)); }
            catch (_) { return []; }
        };
        const visible = node => {
            const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
                && style.display !== "none" && style.visibility !== "hidden"
                && !node.disabled && node.getAttribute("aria-disabled") !== "true";
        };
        const label = node => [
            node.innerText || "", node.textContent || "", node.getAttribute("aria-label") || "",
            node.getAttribute("title") || "", node.getAttribute("rel") || "",
            node.id || "", String(node.className || "")
        ].join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
        const configured = safeAll(selector);
        const semantic = safeAll("a[href],button,[role='button']").filter(node =>
            /next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(label(node))
        );
        const candidates = Array.from(new Set([...semantic, ...configured])).filter(visible);
        candidates.sort((a, b) => {
            const aScore = /next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(label(a)) ? 100 : 0;
            const bScore = /next\\s+(chapter|episode)|(chapter|episode)\\s+next/.test(label(b)) ? 100 : 0;
            return bScore - aScore;
        });
        const anchor = candidates.find(node => node.href);
        return anchor ? anchor.href : "";
    """
    try:
        return str(driver.execute_script(script, selectors.next_chapter) or "")
    except (JavascriptException, WebDriverException):
        return ""


def activate_next_chapter(
    driver: object,
    selectors: ReaderSelectors,
    timeout_seconds: float = 15.0,
) -> Optional[str]:
    before = reader_snapshot(driver, selectors)
    href = next_chapter_href(driver, selectors)
    if href and canonical_chapter_url(href) != canonical_chapter_url(before.url):
        return href
    if not _click_control(
        driver,
        selectors.next_chapter,
        control_kind="chapter",
        before=before,
    ):
        return None
    after = wait_for_reader_change(driver, before, selectors, timeout_seconds)
    if after and canonical_chapter_url(after.url) != canonical_chapter_url(before.url):
        return after.url
    return None


def chapter_number_from_text(value: str) -> Optional[float]:
    matches = re.findall(
        r"(?:chapter|ch\.?|episode|ep\.?)\s*[-:#]?\s*(\d+(?:\.\d+)?)",
        value,
        re.IGNORECASE,
    )
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def next_chapter_from_visible_list(
    driver: object,
    selectors: ReaderSelectors,
    current_url: str,
    current_title: str = "",
) -> Optional[str]:
    script = """
        const selector = arguments[0];
        let nodes = [];
        try { nodes = Array.from(document.querySelectorAll(selector)); }
        catch (_) { return []; }
        return nodes.map(node => ({
            href: node.href || node.value || node.getAttribute("data-href") || "",
            label: [
                node.innerText || "", node.textContent || "",
                node.getAttribute("aria-label") || "", node.getAttribute("title") || ""
            ].join(" ").replace(/\\s+/g, " ").trim()
        })).filter(item => item.href);
    """
    try:
        candidates = driver.execute_script(script, selectors.chapter_list) or []
    except (JavascriptException, WebDriverException):
        return None

    current_key = canonical_chapter_url(current_url)
    current_number = chapter_number_from_text(current_title) or chapter_number_from_text(current_url)
    parsed: list[tuple[float, str]] = []
    ordered: list[str] = []
    for item in candidates:
        href = str(item.get("href", "")).strip()
        label = str(item.get("label", "")).strip()
        if not href or canonical_chapter_url(href) == current_key:
            continue
        if href not in ordered:
            ordered.append(href)
        number = chapter_number_from_text(f"{label} {href}")
        if number is not None:
            parsed.append((number, href))
    if current_number is not None:
        later = sorted(item for item in parsed if item[0] > current_number)
        if later:
            return later[0][1]
    return ordered[0] if ordered else None
