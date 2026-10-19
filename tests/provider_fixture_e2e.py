#!/usr/bin/env python3
"""Browser-level E2E checks against a local provider-shaped fake site.

The fake site intentionally includes a series-page comment box, chapter links,
infinite-scroll chapter blocks, thanks buttons, and a broken submit case. The
bot is run in headless mode with an isolated Chrome profile and local JSONL
files only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from history_utils import read_jsonl_records


BOT = APP_DIR / "bot.py"
REPORT_DIR = APP_DIR / "outputs/test-reports"
DEFAULT_REPORT_PATH = REPORT_DIR / "panelyra_provider_e2e_report.json"


class ProviderFixtureHandler(BaseHTTPRequestHandler):
    server_version = "PanelyraFixture/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/reader/paged":
            try:
                page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            except ValueError:
                page = 1
            html = paged_reader_page(page)
            status = 200 if page in {1, 2} else 404
        else:
            html = ROUTES.get(parsed.path, not_found_page(parsed.path))
            status = 200 if parsed.path in ROUTES else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} - Panelyra Fixture</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #080808; color: #f4f4f5; }}
    header {{ position: sticky; top: 0; background: #111; padding: 14px 24px; z-index: 2; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 32px 20px 160px; }}
    a {{ color: #d8d8d8; }}
    .hero, .chapter, .comments, .chapter-card {{ border: 1px solid #333; padding: 24px; margin: 24px 0; background: #121212; }}
    .spacer {{ height: 760px; background: linear-gradient(#161616, #0d0d0d); margin: 24px 0; }}
    textarea {{ display: block; width: 96%; min-height: 90px; margin: 12px 0; font-size: 16px; }}
    button {{ padding: 10px 16px; font-weight: 700; }}
    .posted-comment, .comment-success {{ margin-top: 12px; padding: 12px; background: #1d3325; border: 1px solid #4ade80; }}
    .error-box {{ margin-top: 12px; padding: 12px; background: #3b1111; border: 1px solid #f87171; }}
  </style>
  <script>
    function submitComment(button) {{
      const root = button.closest('[data-comment-root]') || document;
      const editor = root.querySelector('textarea, [contenteditable="true"], [role="textbox"]');
      const text = (editor.value || editor.innerText || editor.textContent || '').trim();
      const target = root.querySelector('[data-comments]') || root;
      if (!text) {{
        const error = document.createElement('div');
        error.className = 'error-box';
        error.textContent = 'Please enter a comment before sending.';
        target.appendChild(error);
        return;
      }}
      if (root.getAttribute('data-broken-submit') === 'true') {{
        const error = document.createElement('div');
        error.className = 'error-box';
        error.textContent = 'Submission failed intentionally for test coverage.';
        target.appendChild(error);
        return;
      }}
      const comment = document.createElement('div');
      comment.className = 'posted-comment';
      comment.textContent = text;
      const success = document.createElement('div');
      success.className = 'comment-success';
      success.textContent = 'Comment posted successfully';
      target.appendChild(comment);
      target.appendChild(success);
      editor.value = '';
      editor.textContent = '';
    }}
    function sayThanks(button) {{
      button.textContent = 'Already thanked';
      button.setAttribute('aria-disabled', 'true');
    }}
  </script>
</head>
<body>
  <header><strong>Chapter Fixture</strong> <span>What are we looking for today?</span></header>
  <main>{body}</main>
</body>
</html>"""


def comment_block(label: str, *, broken: bool = False) -> str:
    broken_attr = " data-broken-submit='true'" if broken else ""
    return f"""
<section class="comments" data-comment-root{broken_attr}>
  <h2>Comments</h2>
  <textarea name="comment" placeholder="Write your comment..." aria-label="comment box for {label}"></textarea>
  <button type="button" onclick="submitComment(this)">Send</button>
  <div data-comments></div>
</section>"""


def series_page() -> str:
    return layout(
        "Apotheosis",
        """
<section class="hero">
  <img alt="Poster Apotheosis" src="/static/poster.png">
  <h1>Apotheosis</h1>
  <p>Apotheosis - Ascension to Godhood / Apotheosis - Elevation to the Status of a God</p>
  <p>Release year 2015. Original country China. Original status Ongoing. Chapters 1297.</p>
  <a href="/content/apotheosis/chapter-1">Read from the first chapter</a>
  <button>Add to folder</button>
</section>
<section>
  <h2>Description</h2>
  <p>Luo Zheng, now a humble slave, finds an ancient book and a path against fate.</p>
</section>
<section>
  <h2>Chapter list</h2>
  <a class="chapter-card" href="/content/apotheosis/chapter-1">Vol. 1 Ch. 1</a>
  <a class="chapter-card" href="/content/apotheosis/chapter-2">Vol. 1 Ch. 2</a>
</section>
"""
        + comment_block("series-trap"),
    )


def chapter_page(number: int, *, broken: bool = False) -> str:
    next_link = f'<a class="next-chapter" rel="next" href="/content/apotheosis/chapter-{number + 1}">Next Chapter</a>' if number == 1 else ""
    return layout(
        f"Apotheosis Chapter {number}",
        f"""
<div class="advert-overlay modal" role="dialog" aria-modal="true"
     style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.82);display:grid;place-items:center">
  <div style="background:#fff;color:#111;padding:30px;min-width:320px">
    <button type="button" aria-label="Close advertisement"
            onclick="this.closest('.advert-overlay').remove()">×</button>
    <p>Sponsored interruption</p>
  </div>
</div>
<article class="chapter-content">
  <h1>Apotheosis Chapter {number}</h1>
  <p>Luo Zheng studies the divine technique while the clan watches his strategy and reputation.</p>
  <p>The chapter builds tension around betrayal, identity, and the quiet clue hidden in the final panel.</p>
  <div class="spacer"></div>
</article>
<button type="button" onclick="sayThanks(this)">Say thanks</button>
{comment_block(f'chapter-{number}', broken=broken)}
{next_link}
""",
    )


def infinite_page() -> str:
    chapters = []
    for number in (1, 2):
        chapters.append(
            f"""
<section class="chapter">
  <article class="chapter-content">
    <h1>Infinite Apotheosis Chapter {number}</h1>
    <p>This stacked chapter focuses on strategy, loyalty, and the pressure around the hidden clue.</p>
    <p>The panel flow makes the final decision feel like a turning point for Luo Zheng.</p>
    <div class="spacer"></div>
  </article>
  <button type="button" onclick="sayThanks(this)">Say thanks</button>
  {comment_block(f'infinite-{number}')}
</section>
"""
        )
    return layout("Apotheosis Infinite Reader", "\n".join(chapters))


def paged_reader_page(number: int) -> str:
    next_disabled = " disabled aria-disabled='true'" if number >= 2 else ""
    next_target = min(number + 1, 2)
    return layout(
        f"Apotheosis Paged Reader {number}",
        f"""
<article class="chapter-content">
  <h1>Vol. 1 Ch. {number}</h1>
  <p>This page focuses on a distinct chapter about loyalty, strategy, and consequences.</p>
  <div class="spacer"></div>
</article>
<button type="button" onclick="sayThanks(this)">Say thanks</button>
{comment_block(f'paged-{number}')}
<nav aria-label="reader pages" style="display:flex;gap:12px;align-items:center">
  <button type="button" role="combobox">{number} / 2</button>
  <button type="button" aria-label="next page" onclick="window.location.href='/reader/paged?page={next_target}'"{next_disabled}>Next</button>
</nav>
""",
    )


def not_found_page(path: str) -> str:
    return layout("Not Found", f"<h1>404</h1><p>No fake route for {path}</p>")


ROUTES = {
    "/content/apotheosis": series_page(),
    "/content/apotheosis/chapter-1": chapter_page(1),
    "/content/apotheosis/chapter-2": chapter_page(2),
    "/content/apotheosis/broken-submit": chapter_page(99, broken=True),
    "/reader/infinite": infinite_page(),
}


class ScenarioRunner:
    def __init__(self, base_url: str, tmpdir: Path, python: Path) -> None:
        self.base_url = base_url
        self.tmpdir = tmpdir
        self.python = python
        self.results: list[dict[str, object]] = []

    def run(
        self,
        name: str,
        args: list[str],
        expect_submitted: int,
        expect_failed: int = 0,
        expect_returncode: int = 0,
        preloaded_markers: list[dict[str, object]] | None = None,
    ) -> None:
        only_scenario = (
            os.getenv("PANELYRA_E2E_ONLY", "").strip()
            or os.getenv("ASURA_E2E_ONLY", "").strip()
        )
        if only_scenario and name != only_scenario:
            return
        scenario_dir = self.tmpdir / name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        history_file = scenario_dir / "history.jsonl"
        marker_file = scenario_dir / "markers.jsonl"
        log_file = scenario_dir / "bot.log"
        profile_dir = scenario_dir / "chrome-profile"
        if preloaded_markers:
            marker_file.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in preloaded_markers),
                encoding="utf-8",
            )
        command = [
            str(self.python),
            str(BOT),
            "--history-file",
            str(history_file),
            "--read-marker-file",
            str(marker_file),
            "--log-file",
            str(log_file),
            "--headless",
            "--chrome-profile-dir",
            str(profile_dir),
            "--content-mode",
            "novel",
            "--content-container-selector",
            "article, main, .chapter-content, .content",
            "--scroll-min-px",
            "900",
            "--scroll-max-px",
            "1400",
            "--scroll-min-delay",
            "0",
            "--scroll-max-delay",
            "0",
            "--type-min-delay",
            "0",
            "--type-max-delay",
            "0",
            "--visible-type-chars",
            "0",
            "--reuse-loaded-images",
            "true",
            "--cooldown-min-seconds",
            "0",
            "--cooldown-max-seconds",
            "0",
            "--post-submit-wait-seconds",
            "2",
            *args,
        ]
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(self.tmpdir / "pycache")
        completed = subprocess.run(
            command,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
        records = read_jsonl_records(history_file)
        submitted = [record for record in records if record.get("status") == "submitted"]
        failed = [record for record in records if record.get("status") == "failed"]
        ok = (
            completed.returncode == expect_returncode
            and len(submitted) == expect_submitted
            and len(failed) == expect_failed
        )
        self.results.append(
            {
                "name": name,
                "ok": ok,
                "returncode": completed.returncode,
                "expected_returncode": expect_returncode,
                "expected_submitted": expect_submitted,
                "actual_submitted": len(submitted),
                "expected_failed": expect_failed,
                "actual_failed": len(failed),
                "log_file": str(log_file),
                "history_file": str(history_file),
                "stdout_tail": completed.stdout[-4000:],
            }
        )

    def assert_all_ok(self) -> None:
        failures = [result for result in self.results if not result["ok"]]
        if failures:
            raise AssertionError(json.dumps(failures, indent=2, ensure_ascii=False))


def serve_fake_site() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Panelyra provider-fixture browser E2E scenarios.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()

    python = Path(sys.executable)
    server, base_url = serve_fake_site()
    try:
        with tempfile.TemporaryDirectory(prefix="panelyra-provider-e2e-") as raw_tmpdir:
            tmpdir = Path(raw_tmpdir)
            runner = ScenarioRunner(base_url, tmpdir, python)
            runner.run(
                "series_trap_next_link_success",
                [
                    "--start-url",
                    f"{base_url}/content/apotheosis",
                    "--max-chapters",
                    "2",
                    "--chapter-routing",
                    "next-link",
                ],
                expect_submitted=2,
            )
            runner.run(
                "infinite_scroll_with_thanks_success",
                [
                    "--start-url",
                    f"{base_url}/reader/infinite?page=18",
                    "--max-chapters",
                    "2",
                    "--chapter-routing",
                    "infinite-scroll",
                    "--use-thanks-marker",
                ],
                expect_submitted=2,
            )
            runner.run(
                "broken_submit_records_failure",
                [
                    "--start-url",
                    f"{base_url}/content/apotheosis/broken-submit",
                    "--max-chapters",
                    "1",
                    "--chapter-routing",
                    "next-link",
                ],
                expect_submitted=0,
                expect_failed=1,
                expect_returncode=1,
            )
            runner.run(
                "paged_reader_next_control_success",
                [
                    "--start-url",
                    f"{base_url}/reader/paged?page=1",
                    "--max-chapters",
                    "2",
                    "--chapter-routing",
                    "infinite-scroll",
                    "--use-thanks-marker",
                ],
                expect_submitted=2,
            )
            runner.run(
                "paged_reader_marker_skip_success",
                [
                    "--start-url",
                    f"{base_url}/reader/paged?page=1",
                    "--max-chapters",
                    "1",
                    "--chapter-routing",
                    "infinite-scroll",
                    "--use-thanks-marker",
                ],
                expect_submitted=1,
                preloaded_markers=[
                    {
                        "chapter_key": f"{base_url}/reader/paged",
                        "url": f"{base_url}/reader/paged?page=1",
                        "status": "read",
                    }
                ],
            )
            report = {
                "base_url": base_url,
                "total": len(runner.results),
                "passed": sum(1 for result in runner.results if result["ok"]),
                "failed": sum(1 for result in runner.results if not result["ok"]),
                "results": runner.results,
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({key: report[key] for key in ("total", "passed", "failed")}, indent=2))
            print(f"Report: {args.report}")
            runner.assert_all_ok()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
