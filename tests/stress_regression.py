#!/usr/bin/env python3
"""Deterministic 1000-case local stress checks for the dashboard and bot.

This intentionally avoids live posting. It exercises generation, persistence,
URL normalization, local CLI validation, and dependency-facing startup paths.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import bot
import qa_utils
from history_utils import append_jsonl_record, read_jsonl_records


REPORT_DIR = APP_DIR / "test_reports"
DEFAULT_REPORT_PATH = REPORT_DIR / "panelyra_stress_report.json"
RANDOM_SEED = 20260716


class StressRunner:
    def __init__(self) -> None:
        self.total = 0
        self.failures: list[dict[str, object]] = []
        self.category_counts: dict[str, int] = {}
        self.category_failures: dict[str, int] = {}

    def run(self, category: str, name: str, check: Callable[[], None]) -> None:
        self.total += 1
        self.category_counts[category] = self.category_counts.get(category, 0) + 1
        try:
            check()
        except Exception as exc:
            self.category_failures[category] = self.category_failures.get(category, 0) + 1
            self.failures.append(
                {
                    "case": self.total,
                    "category": category,
                    "name": name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    def report(self) -> dict[str, object]:
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seed": RANDOM_SEED,
            "total_cases": self.total,
            "passed": self.total - len(self.failures),
            "failed": len(self.failures),
            "category_counts": self.category_counts,
            "category_failures": self.category_failures,
            "failures": self.failures,
        }


def assert_comment_shape(text: str) -> None:
    if not text.strip():
        raise AssertionError("comment is blank")
    if len(text) > 300:
        raise AssertionError(f"comment exceeds 300 characters: {len(text)}")
    if "\n" in text or "\r" in text:
        raise AssertionError("comment contains line breaks")
    if "  " in text:
        raise AssertionError("comment contains repeated spaces")
    if any(ord(char) > 0xFFFF for char in text):
        raise AssertionError("comment contains non-BMP characters unsupported by ChromeDriver")


def random_context(index: int) -> bot.ChapterContext:
    keyword_pool = [
        "setup",
        "betrayal",
        "strategy",
        "identity",
        "tension",
        "dialogue",
        "panel",
        "motive",
        "signal",
        "aftershock",
        "choice",
        "reputation",
        "parallel",
        "clue",
        "stakes",
    ]
    random.shuffle(keyword_pool)
    keywords = keyword_pool[: random.randint(1, 8)]
    title = f"Stress Chapter {index} - {random.choice(keywords).title()}"
    excerpt = " ".join(
        [
            f"The chapter keeps returning to {keyword}."
            for keyword in keywords[:4]
        ]
    )
    return bot.ChapterContext(title=title, keywords=keywords, excerpt=excerpt, text_length=len(excerpt))


def contextual_generation_case(index: int) -> None:
    used_comments: set[str] = set()
    history = [
        {"comment": f"Previous local comment {index}-{offset}", "style": random.choice(bot.COMMENT_STYLES)}
        for offset in range(random.randint(0, 8))
    ]
    research = None
    if index % 7 == 0:
        research = bot.ResearchContext(
            source="local:stress",
            text="The local research note points at recurring strategy and reputation pressure.",
            matched_terms=["strategy", "reputation"],
        )
    generated = bot.build_contextual_comment(
        random_context(index),
        index,
        used_comments,
        history,
        research,
        reflection_every_n_comments=random.randint(2, 12),
    )
    assert_comment_shape(generated.text)
    if generated.style not in set(bot.COMMENT_STYLES) | {"fallback"}:
        raise AssertionError(f"unknown style: {generated.style}")
    if generated.research_source and generated.research_source != "local:stress":
        raise AssertionError(f"unexpected research source: {generated.research_source}")


def generic_generation_case(index: int, used_comments: set[str]) -> None:
    text = bot.generate_comment(index, used_comments)
    assert_comment_shape(text)
    if not bot.comment_signature(text):
        raise AssertionError("generated comment has no searchable signature")


def qa_unique_generation_case(index: int, used_comments: set[str]) -> None:
    text = qa_utils.generate_unique_comment(
        iteration=index,
        used_comments=used_comments,
        opening_phrases=bot.OPENING_REMARKS,
        subject_phrases=bot.SUBJECT_PHRASES,
        ending_phrases=bot.ENDING_PHRASES,
        joiners=bot.JOINERS,
        punctuation=bot.END_PUNCTUATION,
        add_suffix=index % 3 == 0,
    )
    assert_comment_shape(bot.finalize_comment_text(text))


def finalization_case(index: int) -> None:
    samples = [
        "This chapter -- really can't be ignored!!!",
        "  Weird    spacing with\ttabs and a long thought about setup  ",
        "Doesn't this feel like a clue - or maybe a fakeout?",
        f"Case {index}: strategy, motive, and payoff all connect.",
    ]
    text = bot.finalize_comment_text(random.choice(samples), max_length=random.randint(40, 180))
    assert_comment_shape(text)
    if "-" in text:
        raise AssertionError("dash normalization did not remove hyphen")


def persistence_case(tmpdir: Path, index: int) -> None:
    path = tmpdir / f"history-{index % 11}.jsonl"
    record = {
        "index": index,
        "path": tmpdir / "nested" / str(index),
        "status": random.choice(["submitted", "failed", "skipped"]),
        "comment": f"Persistence comment {index}",
    }
    append_jsonl_record(path, record)
    records = read_jsonl_records(path)
    if not records:
        raise AssertionError("no JSONL records were read back")
    if not isinstance(records[-1].get("path"), str):
        raise AssertionError("non-JSON value was not stringified")


def url_case(index: int) -> None:
    url = (
        f"https://example.com/content/series-{index % 9}/chapter-{index}"
        f"?page={index % 5}&scroll={index}&keep={index % 3}&comment=true"
    )
    canonical = bot.canonical_chapter_key(url)
    if "page=" in canonical or "scroll=" in canonical or "comment=" in canonical:
        raise AssertionError(f"canonical URL kept transient parameters: {canonical}")
    if f"keep={index % 3}" not in canonical:
        raise AssertionError(f"canonical URL dropped stable query parameter: {canonical}")

    read_marker_key = bot.canonical_read_marker_key(url)
    if f"page={index % 5}" not in read_marker_key:
        raise AssertionError(f"read marker dropped the numbered reader page: {read_marker_key}")
    next_page_url = url.replace(f"page={index % 5}", f"page={(index % 5) + 1}")
    if bot.canonical_read_marker_key(next_page_url) == read_marker_key:
        raise AssertionError("different reader pages collapsed to the same read marker")

    stacked_marker = bot.canonical_read_marker_key(
        f"https://example.com/reader/{index}",
        marker_y=1200 + index,
    )
    if not stacked_marker.endswith(f"#y={1200 + index}"):
        raise AssertionError(f"stacked-reader marker lost its vertical position: {stacked_marker}")
    series_url = bot.series_url_from_chapter_url(url)
    if series_url != f"https://example.com/content/series-{index % 9}":
        raise AssertionError(f"series URL mismatch: {series_url}")


def ad_noise_filter_case(index: int) -> None:
    text = (
        "Remove ads and popups now. Sponsored promotion banner. "
        "The character choice changes the pacing and raises the stakes."
    )
    keywords = bot.extract_keywords(text, limit=8)
    banned = bot.COMMENT_BANNED_WORDS
    leaked = [keyword for keyword in keywords if bot.normalize_keyword(keyword) in banned]
    if leaked:
        raise AssertionError(f"ad-related keyword leaked: {leaked}")

    comment = bot.finalize_comment_text(f"These ads made the chapter feel noisy in case {index}.")
    if bot.contains_banned_comment_word(comment):
        raise AssertionError(f"ad-related word leaked into finalized comment: {comment}")

    clean_title = bot.sanitize_comment_subject_text("Ads blocked the reader", fallback="this chapter")
    if clean_title != "this chapter":
        raise AssertionError(f"ad-related title was not sanitized: {clean_title}")


def subprocess_case(command: list[str], expected_returncode: int, expected_text: str) -> None:
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(Path(tempfile.gettempdir()) / "panelyra_stress_pycache")
    completed = subprocess.run(
        command,
        cwd=APP_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
        env=env,
    )
    if completed.returncode != expected_returncode:
        raise AssertionError(f"return code {completed.returncode}, expected {expected_returncode}: {completed.stdout}")
    if expected_text not in completed.stdout:
        raise AssertionError(f"expected text {expected_text!r} not found in output: {completed.stdout}")


def compile_case() -> None:
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(Path(tempfile.gettempdir()) / "panelyra_stress_pycache")
    files = [
        "app.py",
        "bot.py",
        "qa_utils.py",
        "history_utils.py",
        "chapter_content_extractor.py",
        "note_art_bot.py",
        "comment_flow_qa.py",
        "reader_routing.py",
        "panelyra/app.py",
        "panelyra/models/product.py",
        "panelyra/services/health.py",
        "panelyra/services/paths.py",
        "panelyra/services/preferences.py",
        "panelyra/services/presets.py",
        "panelyra/services/storage.py",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "py_compile", *files],
        cwd=APP_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 1000 local stress checks and record failures.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()

    random.seed(RANDOM_SEED)
    runner = StressRunner()
    generic_used: set[str] = set()
    qa_used: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="panelyra-stress-") as raw_tmpdir:
        tmpdir = Path(raw_tmpdir)

        for index in range(320):
            runner.run("contextual_comment_generation", f"contextual-{index}", lambda index=index: contextual_generation_case(index))
        for index in range(160):
            runner.run("generic_comment_generation", f"generic-{index}", lambda index=index: generic_generation_case(index, generic_used))
        for index in range(160):
            runner.run("qa_unique_generation", f"qa-unique-{index}", lambda index=index: qa_unique_generation_case(index, qa_used))
        for index in range(120):
            runner.run("comment_finalization", f"finalize-{index}", lambda index=index: finalization_case(index))
        for index in range(120):
            runner.run("jsonl_persistence", f"jsonl-{index}", lambda index=index: persistence_case(tmpdir, index))
        for index in range(85):
            runner.run("url_normalization", f"url-{index}", lambda index=index: url_case(index))
        for index in range(10):
            runner.run("ad_noise_filtering", f"ad-noise-{index}", lambda index=index: ad_noise_filter_case(index))

        python = sys.executable
        subprocess_checks = [
            ([python, "bot.py", "--start-url", "https://example.com", "--max-chapters", "0"], 2, "--max-chapters must be at least 1"),
            ([python, "bot.py", "--start-url", "https://example.com", "--max-chapters", "1", "--scroll-min-px", "0"], 2, "Scroll pixel settings are invalid"),
            ([python, "bot.py", "--start-url", "https://example.com", "--max-chapters", "1", "--type-min-delay", "1", "--type-max-delay", "0"], 2, "Typing delay settings are invalid"),
            ([python, "note_art_bot.py", "--note-url", "https://example.com", "--image-path", str(tmpdir / "missing.png"), "--word-art-text", "test"], 2, "Image file does not exist"),
            ([python, "comment_flow_qa.py", "--max-comments", "0", "--comment-mode", "bad"], 2, "invalid choice"),
        ]
        for index in range(20):
            command, code, text = subprocess_checks[index % len(subprocess_checks)]
            runner.run("cli_validation", f"cli-{index}", lambda command=command, code=code, text=text: subprocess_case(command, code, text))

        for index in range(5):
            runner.run("python_compile", f"compile-{index}", compile_case)

    report = runner.report()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({key: report[key] for key in ("total_cases", "passed", "failed", "category_failures")}, indent=2))
    print(f"Report: {args.report}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
