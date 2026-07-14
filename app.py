#!/usr/bin/env python3
"""
Local Streamlit dashboard for running the sequential form-submission QA suite.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

import streamlit as st

from history_utils import read_jsonl_records


APP_DIR = Path(__file__).resolve().parent
BOT_PATH = APP_DIR / "bot.py"
BOT_PYTHON = APP_DIR / ".venv" / "bin" / "python"
HISTORY_FILE = APP_DIR / "comment_history.jsonl"
READ_MARKER_FILE = APP_DIR / "read_markers.jsonl"
RESEARCH_FILE = APP_DIR / "research_context.json"
LOG_DIR = Path(tempfile.gettempdir()) / "asura_automation_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONSOLE_CHARS = 60_000


def is_process_running(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


def read_log(log_path: Path) -> str:
    if not log_path.exists():
        return "Waiting for automation output..."
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= MAX_CONSOLE_CHARS:
        return text
    return f"... showing latest {MAX_CONSOLE_CHARS:,} characters ...\n\n{text[-MAX_CONSOLE_CHARS:]}"


def read_history() -> list[dict[str, object]]:
    return read_jsonl_records(HISTORY_FILE)


COMMENT_STYLES = [
    "question",
    "theory",
    "reaction",
    "joke",
    "prediction",
    "detail",
    "foreshadowing",
    "character_focus",
    "pacing",
    "cliffhanger",
    "worldbuilding",
    "mystery",
    "emotional",
    "comparison",
    "callback",
    "suspicion",
    "appreciation",
    "tension",
    "confusion",
    "reread",
    "favorite_moment",
    "next_chapter_hook",
    "plot_twist",
    "panel_art",
    "dialogue",
    "power_scaling",
    "villain_watch",
    "hero_moment",
    "side_character",
    "relationship",
    "strategy",
    "lore_question",
    "mood_shift",
    "slow_burn",
    "payoff",
    "setup",
    "reader_poll",
    "wild_guess",
    "respect",
    "quiet_detail",
]


def style_recommendation(records: list[dict[str, object]]) -> str:
    if not records:
        return "No comment history yet. Run a short test first so the analyzer has data."

    counts = Counter(str(record.get("style", "unknown")) for record in records)
    lowest_count = min(counts.get(style, 0) for style in COMMENT_STYLES)
    underused = [style for style in COMMENT_STYLES if counts.get(style, 0) == lowest_count]

    emoji_rate = sum(1 for record in records if record.get("emoji_used")) / max(1, len(records))
    advice = [f"Next comment should lean toward: {', '.join(underused)}."]
    if emoji_rate > 0.35:
        advice.append("Emoji use is getting high; prefer no emoji for the next few comments.")
    elif emoji_rate < 0.08 and len(records) >= 10:
        advice.append("Emoji use is very low; one subtle emoji is acceptable if the chapter tone fits.")

    repeated_keywords = Counter(
        str(record.get("keyword", "")).lower()
        for record in records
        if str(record.get("keyword", "")).strip()
    )
    if repeated_keywords:
        keyword, count = repeated_keywords.most_common(1)[0]
        if count >= 3:
            advice.append(f"The keyword '{keyword}' has appeared often; vary away from it when possible.")

    return " ".join(advice)


def stop_bot(process: subprocess.Popen | None, timeout_seconds: float = 8.0) -> None:
    if not is_process_running(process):
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()


def start_bot(
    start_url: str,
    max_chapters: int,
    wait_for_login: bool,
    research_file: str,
    research_endpoint: str,
    use_thanks_marker: bool,
    chapter_routing: str,
    scroll_min_px: int,
    scroll_max_px: int,
    scroll_min_delay: float,
    scroll_max_delay: float,
) -> tuple[subprocess.Popen, Path, Path | None]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"sequential-qa-{timestamp}.log"
    signal_path = LOG_DIR / f"start-signal-{timestamp}-{uuid.uuid4().hex}.txt" if wait_for_login else None
    python_executable = str(BOT_PYTHON if BOT_PYTHON.exists() else Path(sys.executable))
    command = [
        python_executable,
        str(BOT_PATH),
        "--start-url",
        start_url,
        "--max-chapters",
        str(max_chapters),
        "--history-file",
        str(HISTORY_FILE),
        "--read-marker-file",
        str(READ_MARKER_FILE),
        "--chapter-routing",
        chapter_routing,
        "--scroll-min-px",
        str(scroll_min_px),
        "--scroll-max-px",
        str(scroll_max_px),
        "--scroll-min-delay",
        str(scroll_min_delay),
        "--scroll-max-delay",
        str(scroll_max_delay),
    ]
    if research_file.strip():
        command.extend(["--research-file", research_file.strip()])
    if research_endpoint.strip():
        command.extend(["--research-endpoint", research_endpoint.strip()])
    if use_thanks_marker:
        command.append("--use-thanks-marker")
    if signal_path:
        command.extend(["--start-signal-file", str(signal_path)])

    startup_banner = (
        "Starting bot process...\n"
        f"Working directory: {APP_DIR}\n"
        f"Python: {python_executable}\n"
        f"Command: {' '.join(command)}\n\n"
    )
    log_path.write_text(startup_banner, encoding="utf-8")
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    process = subprocess.Popen(
        command,
        cwd=str(APP_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    log_handle.close()
    return process, log_path, signal_path


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;520;650;760&family=Playfair+Display:wght@650;720&display=swap');
        :root {
            --bg: #050505;
            --surface: #0a0a0b;
            --surface-2: #101012;
            --surface-3: #151517;
            --line: rgba(255,255,255,0.10);
            --line-strong: rgba(255,255,255,0.18);
            --text: #f5f5f4;
            --muted: #a1a1aa;
            --muted-2: #71717a;
            --accent: #e7e5e4;
            --success: #d6d3d1;
            --warning: #f5f5f4;
            --shadow-lg: 0 28px 80px rgba(0,0,0,0.52);
            --shadow-md: 0 18px 48px rgba(0,0,0,0.38);
        }
        html, body, [data-testid="stAppViewContainer"], .main {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.035) 0%, rgba(255,255,255,0) 26%),
                linear-gradient(135deg, #050505 0%, #0b0b0c 46%, #050505 100%);
            color: var(--text);
            font-family: Manrope, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .block-container {
            padding: 1.25rem 1.75rem 2.25rem 1.75rem;
            max-width: 1360px;
            position: relative;
        }
        .block-container::before {
            content: "";
            position: absolute;
            top: 0;
            left: 1.75rem;
            right: 1.75rem;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.42), transparent);
            pointer-events: none;
        }
        section[data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.085) 0%, rgba(255,255,255,0.025) 100%),
                rgba(10,10,11,0.72);
            border-right: 1px solid rgba(255,255,255,0.16);
            backdrop-filter: blur(26px) saturate(145%);
            -webkit-backdrop-filter: blur(26px) saturate(145%);
            box-shadow: inset -1px 0 rgba(255,255,255,0.05), 16px 0 60px rgba(0,0,0,0.24);
        }
        section[data-testid="stSidebar"] * {
            color: var(--text);
        }
        section[data-testid="stSidebar"] code {
            color: var(--text);
            background: #101010;
            border: 1px solid var(--line);
            border-radius: 8px;
        }
        h1, h2, h3, h4, h5, h6, p, label, span {
            color: var(--text);
            letter-spacing: 0;
        }
        h1, h2, h3 {
            font-family: "Playfair Display", ui-serif, Georgia, serif;
        }
        div[data-testid="stMetric"] {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.115) 0%, rgba(255,255,255,0.028) 100%),
                var(--surface-2);
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 1rem 1.05rem;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.11),
                0 28px 70px rgba(0,0,0,0.46),
                0 8px 24px rgba(0,0,0,0.34);
        }
        div[data-testid="stMetric"] [data-testid="stMetricLabel"],
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: var(--text);
            font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
        }
        div.stButton > button {
            border-radius: 999px;
            min-height: 3rem;
            font-weight: 650;
            border: 1px solid var(--line-strong);
            background:
                linear-gradient(180deg, rgba(255,255,255,0.11) 0%, rgba(255,255,255,0.045) 100%),
                #101010;
            color: var(--text);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.16),
                inset 0 -1px rgba(0,0,0,0.35),
                0 18px 42px rgba(0,0,0,0.30);
            transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1), border-color 160ms ease, background 160ms ease;
        }
        div.stButton > button:hover {
            background:
                linear-gradient(180deg, rgba(255,255,255,0.18) 0%, rgba(255,255,255,0.07) 100%),
                #171717;
            border-color: rgba(255,255,255,0.32);
            color: #ffffff;
            transform: translateY(-1px);
        }
        div.stButton > button:active {
            transform: scale(0.96) translateY(1px);
        }
        section[data-testid="stSidebar"] div.stButton > button {
            background: transparent;
            color: var(--text);
            border: 1px solid transparent;
            justify-content: flex-start;
            box-shadow: none;
            position: relative;
            overflow: hidden;
        }
        section[data-testid="stSidebar"] div.stButton > button::before {
            content: "";
            position: absolute;
            inset: 4px 6px;
            border-radius: 999px;
            background: linear-gradient(180deg, rgba(255,255,255,0.14), rgba(255,255,255,0.055));
            opacity: 0;
            transform: translateY(12px) scale(0.96);
            transition: opacity 360ms cubic-bezier(0.16, 1, 0.3, 1), transform 360ms cubic-bezier(0.16, 1, 0.3, 1);
            z-index: -1;
        }
        section[data-testid="stSidebar"] div.stButton > button:hover {
            background: transparent;
            border-color: var(--line);
            color: var(--text);
        }
        section[data-testid="stSidebar"] div.stButton > button:hover::before {
            opacity: 1;
            transform: translateY(0) scale(1);
        }
        input, textarea, select, div[data-baseweb="select"] > div {
            background-color: #0b0b0c !important;
            color: var(--text) !important;
            border-color: var(--line-strong) !important;
            border-radius: 12px !important;
        }
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextInput"] input {
            background-color: #0b0b0c !important;
            color: var(--text) !important;
        }
        div[data-testid="stExpander"] {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 14px;
        }
        div[data-testid="stCodeBlock"] {
            border: 1px solid var(--line);
            border-radius: 14px;
            box-shadow: var(--shadow-md);
        }
        .app-shell {
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 1.45rem 1.55rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.035) 100%),
                var(--surface);
            margin-bottom: 1.2rem;
            box-shadow: var(--shadow-lg);
        }
        .page-title {
            font-size: clamp(1.75rem, 2.4vw, 2.85rem);
            font-weight: 760;
            color: var(--text);
            letter-spacing: 0;
            margin-bottom: 0.25rem;
            font-family: "Playfair Display", ui-serif, Georgia, serif;
        }
        .page-subtitle {
            color: var(--muted);
            font-size: 1rem;
            line-height: 1.55;
            max-width: 760px;
        }
        .panel {
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 1.15rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.075) 0%, rgba(255,255,255,0.025) 100%),
                var(--surface);
            margin-bottom: 1rem;
            box-shadow: var(--shadow-md);
        }
        .mac-traffic {
            display: flex;
            gap: 8px;
            align-items: center;
            margin: 0.15rem 0 1.25rem 0;
            height: 18px;
        }
        .traffic-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-grid;
            place-items: center;
            font-size: 8px;
            line-height: 1;
            color: rgba(0,0,0,0);
            box-shadow: inset 0 1px rgba(255,255,255,0.42), 0 2px 8px rgba(0,0,0,0.35);
            transition: color 150ms ease, transform 150ms ease;
            font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
            font-weight: 760;
        }
        .mac-traffic:hover .traffic-dot {
            color: rgba(0,0,0,0.56);
        }
        .traffic-dot:hover {
            transform: scale(1.08);
        }
        .traffic-red { background: #FF5F56; }
        .traffic-yellow { background: #FFBD2E; }
        .traffic-green { background: #27C93F; }
        @property --metric-value {
            syntax: '<integer>';
            initial-value: 0;
            inherits: false;
        }
        @keyframes countUpMetric {
            from { --metric-value: 0; }
            to { --metric-value: var(--metric-target); }
        }
        @keyframes ambientIdlePulse {
            0%, 100% {
                box-shadow:
                    inset 0 1px rgba(255,255,255,0.13),
                    0 26px 70px rgba(0,0,0,0.46),
                    0 0 0 rgba(255,255,255,0.00);
            }
            50% {
                box-shadow:
                    inset 0 1px rgba(255,255,255,0.18),
                    0 30px 82px rgba(0,0,0,0.55),
                    0 0 34px rgba(255,255,255,0.13);
            }
        }
        .metric-card {
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 1rem 1.08rem;
            min-height: 112px;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.118) 0%, rgba(255,255,255,0.032) 100%),
                var(--surface-2);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.12),
                0 30px 74px rgba(0,0,0,0.48),
                0 8px 24px rgba(0,0,0,0.34);
        }
        .metric-card.idle {
            animation: ambientIdlePulse 4.8s ease-in-out infinite;
        }
        .metric-label {
            color: var(--muted);
            font-size: 0.82rem;
            margin-bottom: 0.62rem;
        }
        .metric-value {
            color: var(--text);
            font-size: 1.8rem;
            font-weight: 760;
            letter-spacing: 0;
        }
        .metric-value.count-up {
            --metric-value: 0;
            animation: countUpMetric 900ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
            counter-reset: metric var(--metric-value);
        }
        .metric-value.count-up::after {
            content: counter(metric);
        }
        .metric-helper {
            color: var(--muted-2);
            font-size: 0.78rem;
            margin-top: 0.45rem;
        }
        .panel-title {
            font-weight: 720;
            color: var(--text);
            margin-bottom: 0.35rem;
        }
        .muted {
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.5;
        }
        .pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.45rem;
            margin-top: 0.85rem;
        }
        .pill {
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 0.28rem 0.62rem;
            color: var(--muted);
            background: rgba(255,255,255,0.045);
            font-size: 0.82rem;
        }
        .status-waiting {
            border: 1px solid rgba(255,255,255,0.18);
            background: #14120d;
            padding: 0.9rem 1rem;
            border-radius: 14px;
            color: #f5f5f4;
        }
        .status-ok {
            border: 1px solid rgba(255,255,255,0.18);
            background: #0f1411;
            padding: 0.9rem 1rem;
            border-radius: 14px;
            color: #f5f5f4;
        }
        .primary-action {
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 1.15rem;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.08) 0%, rgba(255,255,255,0.025) 100%),
                var(--surface);
            min-height: 8.4rem;
            box-shadow:
                inset 0 1px rgba(255,255,255,0.11),
                0 30px 72px rgba(0,0,0,0.42),
                0 8px 22px rgba(0,0,0,0.30);
            transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 180ms ease;
        }
        .primary-action:hover {
            transform: translateY(-2px);
            box-shadow:
                inset 0 1px rgba(255,255,255,0.16),
                0 34px 82px rgba(0,0,0,0.50),
                0 0 30px rgba(255,255,255,0.055);
        }
        .primary-action-title {
            font-weight: 720;
            color: var(--text);
            margin-bottom: 0.35rem;
        }
        .primary-action-copy {
            color: var(--muted);
            font-size: 0.9rem;
            line-height: 1.45;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--line);
            border-radius: 14px;
            overflow: hidden;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title: str, subtitle: str, pills: list[str] | None = None) -> None:
    pill_html = ""
    if pills:
        pill_html = "<div class='pill-row'>" + "".join(f"<span class='pill'>{pill}</span>" for pill in pills) + "</div>"
    st.markdown(
        f"""
        <div class="app-shell">
          <div class="page-title">{title}</div>
          <div class="page-subtitle">{subtitle}</div>
          {pill_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_panel(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="panel">
          <div class="panel-title">{title}</div>
          <div class="muted">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: int | str, helper: str = "", idle: bool = False) -> None:
    idle_class = " idle" if idle else ""
    if isinstance(value, int):
        value_html = f"<div class='metric-value count-up' style='--metric-target: {value}'></div>"
    else:
        value_html = f"<div class='metric-value'>{value}</div>"
    st.markdown(
        f"""
        <div class="metric-card{idle_class}">
          <div class="metric-label">{label}</div>
          {value_html}
          <div class="metric-helper">{helper}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_next_step(status_label: str) -> None:
    if st.session_state.start_signal_path:
        message = "Next step: log in if needed, choose your exact chapter in Chrome, then click Start Comments."
        css_class = "status-waiting"
    elif is_process_running(st.session_state.bot_process):
        message = "Run active: watch Live Console for routing, selector diagnostics, and submission status."
        css_class = "status-ok"
    elif status_label.startswith("Finished"):
        message = "Run finished: open Intelligence to review comment history and style balance."
        css_class = "status-ok"
    else:
        message = "Ready: open Run Suite, paste a URL, choose scroll speed, then open Chrome for manual chapter selection."
        css_class = "status-ok"

    st.markdown(f"<div class='{css_class}'>{message}</div>", unsafe_allow_html=True)


def render_button_guide() -> None:
    st.markdown(
        """
        | Button | What it does | When to use it |
        |---|---|---|
        | `Open Browser / Pick Chapter` | Opens Chrome at the starting URL and pauses the bot. | Use this when you need to log in or manually click the chapter first. |
        | `Start Comments` | Releases the paused bot. | Click this only after Chrome is on the exact chapter you want. |
        | `Run Immediately` | Starts the workflow without pausing. | Use this only when the pasted URL is already the exact target page. |
        | `Stop Current Run` | Stops the running bot process. | Use this if the wrong page opens or selectors fail. |
        """,
    )


SCROLL_PRESETS = {
    "Balanced": {
        "min_px": 650,
        "max_px": 1200,
        "min_delay": 0.12,
        "max_delay": 0.35,
        "description": "Fast enough for QA while still visibly moving through the page.",
    },
    "Fast": {
        "min_px": 1400,
        "max_px": 2600,
        "min_delay": 0.05,
        "max_delay": 0.12,
        "description": "Skims pages quickly while preserving a visible scroll sequence.",
    },
    "Ultra fast": {
        "min_px": 5000,
        "max_px": 9000,
        "min_delay": 0.01,
        "max_delay": 0.03,
        "description": "Very aggressive scrolling for staging runs. Typing speed is unchanged.",
    },
    "Manual": {
        "min_px": 650,
        "max_px": 1200,
        "min_delay": 0.12,
        "max_delay": 0.35,
        "description": "Use the exact values from the manual controls.",
    },
}


PAGES = ["Overview", "Run Suite", "Live Console", "Intelligence", "Settings", "Help"]


def set_page(page: str) -> None:
    st.session_state.page = page


st.set_page_config(page_title="Sequential Form QA", page_icon="QA", layout="wide")
inject_styles()

if "bot_process" not in st.session_state:
    st.session_state.bot_process = None
if "log_path" not in st.session_state:
    st.session_state.log_path = None
if "start_signal_path" not in st.session_state:
    st.session_state.start_signal_path = None
if "page" not in st.session_state:
    st.session_state.page = "Overview"
if "start_url" not in st.session_state:
    st.session_state.start_url = ""
if "max_chapters" not in st.session_state:
    st.session_state.max_chapters = 5
if "research_file" not in st.session_state:
    st.session_state.research_file = str(RESEARCH_FILE)
if "research_endpoint" not in st.session_state:
    st.session_state.research_endpoint = ""
if "scroll_preset" not in st.session_state:
    st.session_state.scroll_preset = "Ultra fast"
if "chapter_routing" not in st.session_state:
    st.session_state.chapter_routing = "infinite-scroll"
if "use_thanks_marker" not in st.session_state:
    st.session_state.use_thanks_marker = True

running = is_process_running(st.session_state.bot_process)
status_label = "Running" if running else "Idle"
if st.session_state.bot_process is not None and st.session_state.bot_process.poll() is not None:
    status_label = f"Finished with exit code {st.session_state.bot_process.poll()}"

with st.sidebar:
    st.markdown(
        """
        <div class="mac-traffic">
          <span class="traffic-dot traffic-red">x</span>
          <span class="traffic-dot traffic-yellow">-</span>
          <span class="traffic-dot traffic-green">+</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("### Asura QA")
    st.caption("Automation control")
    render_metric_card("Bot state", status_label, "Local runner", idle=status_label == "Idle")
    st.caption(f"Current page: {st.session_state.page}")
    st.divider()
    for page in PAGES:
        button_label = page
        if st.button(button_label, key=f"nav_{page}", use_container_width=True):
            set_page(page)
            st.rerun()
    st.divider()
    st.caption("App folder")
    st.code(str(APP_DIR), language="text")
    if st.session_state.log_path:
        st.caption("Latest log")
        st.code(str(st.session_state.log_path), language="text")

page = st.session_state.page

if page == "Overview":
    records = read_history()
    submitted = [record for record in records if record.get("status") == "submitted"]
    render_page_header(
        "QA Automation Dashboard",
        "A dark, focused control panel for login-gated chapter testing, scroll tuning, and comment review.",
        ["Start in Run Suite", "Monitor in Console", "Review in Intelligence"],
    )
    render_next_step(status_label)
    metrics = st.columns(4)
    with metrics[0]:
        render_metric_card("Current state", status_label, "Automation process", idle=status_label == "Idle")
    with metrics[1]:
        render_metric_card("Submitted comments", len(submitted), "Successful records")
    with metrics[2]:
        render_metric_card("Comment styles", len(COMMENT_STYLES), "Available modes")
    with metrics[3]:
        render_metric_card("History records", len(records), "JSONL entries")

    workflow_cards = st.columns(4)
    with workflow_cards[0]:
        render_panel("Run Suite", "Paste the starting URL, set chapters, pick scroll speed, then start the workflow.")
    with workflow_cards[1]:
        render_panel("Live Console", "Watch the bot logs, selector diagnostics, generated comments, and routing decisions.")
    with workflow_cards[2]:
        render_panel("Intelligence", "Review comment style balance, keyword repetition, emoji use, and research source history.")
    with workflow_cards[3]:
        render_panel("Help", "Read the plain-language guide for buttons, scroll controls, and research endpoint behavior.")

    quick_cols = st.columns(4)
    if quick_cols[0].button("Go To Run Suite", use_container_width=True):
        set_page("Run Suite")
        st.rerun()
    if quick_cols[1].button("Open Console", use_container_width=True):
        set_page("Live Console")
        st.rerun()
    if quick_cols[2].button("Review Intelligence", use_container_width=True):
        set_page("Intelligence")
        st.rerun()
    if quick_cols[3].button("Read Help", use_container_width=True):
        set_page("Help")
        st.rerun()

elif page == "Run Suite":
    render_page_header(
        "Run Suite",
        "Paste a URL, choose speed, then use one of the large action buttons. Advanced context is optional.",
        ["1. URL", "2. Speed", "3. Start"],
    )
    render_next_step(status_label)

    setup_left, setup_right = st.columns([1.25, 1])
    with setup_left:
        st.markdown("#### 1. Target")
        start_url = st.text_input(
            "Paste starting URL",
            key="start_url",
            placeholder="https://staging.example.com/chapter-1",
            help="Paste chapter 1, or a series page if the bot should attempt to open the first chapter.",
        )
        max_chapters = st.number_input(
            "How many chapters?",
            key="max_chapters",
            min_value=1,
            max_value=10_000,
            step=1,
            help="Hard stop for how many chapter comment sections the bot may process.",
        )
        chapter_routing = st.selectbox(
            "Chapter routing",
            options=["infinite-scroll", "auto", "next-link"],
            key="chapter_routing",
            help="Use infinite-scroll when chapters and comment forms are stacked on one long reader page.",
        )
        use_thanks_marker = st.checkbox(
            "Click Say thanks and mark chapter as read",
            key="use_thanks_marker",
            help="After a successful comment, the bot clicks the chapter Say thanks button and records the chapter URL locally so later runs can skip it.",
        )

        with st.expander("Optional: research context", expanded=False):
            research_file = st.text_input(
                "Local research file",
                key="research_file",
                help="Optional JSON file with title/tags/summary/notes/context fields.",
            )
            research_endpoint = st.text_input(
                "Research endpoint",
                key="research_endpoint",
                placeholder="http://127.0.0.1:8000/research",
                help="Optional endpoint. The bot calls it as endpoint?q=<chapter title and keywords>.",
            )
            st.caption("Leave endpoint blank unless you run a local/private context API.")

    with setup_right:
        st.markdown("#### 2. Scroll Speed")
        scroll_preset = st.selectbox(
            "Scroll speed",
            options=list(SCROLL_PRESETS),
            key="scroll_preset",
            help="Controls only page scrolling. Typing speed stays unchanged.",
        )
        preset = SCROLL_PRESETS[scroll_preset]
        st.caption(preset["description"])

        if scroll_preset == "Manual":
            scroll_min_px = st.number_input("Min scroll px", min_value=1, max_value=50_000, value=650, step=50)
            scroll_max_px = st.number_input("Max scroll px", min_value=1, max_value=50_000, value=1200, step=50)
            scroll_min_delay = st.number_input("Min pause sec", min_value=0.0, max_value=10.0, value=0.12, step=0.01, format="%.2f")
            scroll_max_delay = st.number_input("Max pause sec", min_value=0.0, max_value=10.0, value=0.35, step=0.01, format="%.2f")
        else:
            scroll_min_px = int(preset["min_px"])
            scroll_max_px = int(preset["max_px"])
            scroll_min_delay = float(preset["min_delay"])
            scroll_max_delay = float(preset["max_delay"])

        scroll_settings_valid = scroll_max_px >= scroll_min_px and scroll_max_delay >= scroll_min_delay
        st.metric("Scroll step", f"{int(scroll_min_px)}-{int(scroll_max_px)} px")
        st.metric("Scroll pause", f"{float(scroll_min_delay):.2f}-{float(scroll_max_delay):.2f}s")
        st.metric("Typing speed", "0.05-0.22s per character")
        if not scroll_settings_valid:
            st.error("Scroll max values must be greater than or equal to min values.")

    st.markdown("#### 3. Start")
    action_cols = st.columns(3)
    with action_cols[0]:
        st.markdown(
            "<div class='primary-action'><div class='primary-action-title'>1. Pick chapter first</div><div class='primary-action-copy'>Opens Chrome, loads the URL, then waits while you log in or click the chapter.</div></div>",
            unsafe_allow_html=True,
        )
        open_login_clicked = st.button(
            "Open Browser / Pick Chapter",
            disabled=running or not start_url.strip() or not scroll_settings_valid,
            use_container_width=True,
        )
    with action_cols[1]:
        st.markdown(
            "<div class='primary-action'><div class='primary-action-title'>2. Start on current page</div><div class='primary-action-copy'>Begins only after Chrome is on your selected chapter.</div></div>",
            unsafe_allow_html=True,
        )
        start_comments_clicked = st.button(
            "Start Comments",
            type="primary",
            disabled=not running or st.session_state.start_signal_path is None,
            use_container_width=True,
        )
    with action_cols[2]:
        st.markdown(
            "<div class='primary-action'><div class='primary-action-title'>Exact URL ready?</div><div class='primary-action-copy'>Skip the pause only if the pasted URL is already the exact chapter.</div></div>",
            unsafe_allow_html=True,
        )
        run_clicked = st.button(
            "Run Immediately",
            disabled=running or not start_url.strip() or not scroll_settings_valid,
            use_container_width=True,
        )

    stop_clicked = st.button(
        "Stop Current Run",
        disabled=not running,
        use_container_width=True,
    )

    with st.expander("Button explanations", expanded=False):
        render_button_guide()

    if st.session_state.start_signal_path:
        st.markdown(
            "<div class='status-waiting'>Chrome is paused. Log in if needed, click your desired chapter, then return here and click Start Comments.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='status-ok'>Ready. Use Open Browser / Pick Chapter if you need to choose a chapter manually. Use Run Immediately only for an exact chapter URL.</div>",
            unsafe_allow_html=True,
        )

    if run_clicked:
        st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_bot(
            start_url.strip(),
            int(max_chapters),
            wait_for_login=False,
            research_file=research_file,
            research_endpoint=research_endpoint,
            use_thanks_marker=bool(use_thanks_marker),
            chapter_routing=chapter_routing,
            scroll_min_px=int(scroll_min_px),
            scroll_max_px=int(scroll_max_px),
            scroll_min_delay=float(scroll_min_delay),
            scroll_max_delay=float(scroll_max_delay),
        )
        st.toast("Automation started.")
        set_page("Live Console")
        st.rerun()

    if open_login_clicked:
        st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_bot(
            start_url.strip(),
            int(max_chapters),
            wait_for_login=True,
            research_file=research_file,
            research_endpoint=research_endpoint,
            use_thanks_marker=bool(use_thanks_marker),
            chapter_routing=chapter_routing,
            scroll_min_px=int(scroll_min_px),
            scroll_max_px=int(scroll_max_px),
            scroll_min_delay=float(scroll_min_delay),
            scroll_max_delay=float(scroll_max_delay),
        )
        st.toast("Chrome opened. Pick the chapter, then click Start Comments.")
        set_page("Live Console")
        st.rerun()

    if start_comments_clicked and st.session_state.start_signal_path:
        st.session_state.start_signal_path.write_text("start\n", encoding="utf-8")
        st.session_state.start_signal_path = None
        st.toast("Comment workflow started.")
        set_page("Live Console")
        st.rerun()

    if stop_clicked and is_process_running(st.session_state.bot_process):
        stop_bot(st.session_state.bot_process)
        st.session_state.start_signal_path = None
        st.toast("Stop signal sent.")
        st.rerun()

elif page == "Live Console":
    render_page_header(
        "Live Console",
        "Watch the bot process in real time. Selector failures, generated comments, chapter routing, and login waits appear here.",
        ["Runtime logs", "Selector diagnostics"],
    )
    render_next_step(status_label)
    top = st.columns([1, 1, 1, 1])
    top[0].metric("Bot state", status_label)
    top[1].metric("Log available", "Yes" if st.session_state.log_path else "No")
    if top[2].button(
        "Start Comments",
        disabled=not running or st.session_state.start_signal_path is None,
        use_container_width=True,
    ):
        st.session_state.start_signal_path.write_text("start\n", encoding="utf-8")
        st.session_state.start_signal_path = None
        st.toast("Comment workflow started.")
        st.rerun()
    if top[3].button("Stop", disabled=not running, use_container_width=True):
        stop_bot(st.session_state.bot_process)
        st.session_state.start_signal_path = None
        st.toast("Stop signal sent.")
        st.rerun()

    if st.button("Refresh Console", use_container_width=True):
        st.rerun()
    st.code(read_log(st.session_state.log_path) if st.session_state.log_path else "No run started yet.", language="text")

elif page == "Intelligence":
    records = read_history()
    submitted = [record for record in records if record.get("status") == "submitted"]
    emoji_count = sum(1 for record in records if record.get("emoji_used"))
    render_page_header(
        "Comment Intelligence",
        "Review generated comment history, balance style variety, and inspect repeated keywords.",
        [f"{len(COMMENT_STYLES)} styles", "History analytics", "Research source tracking"],
    )
    metrics = st.columns(4)
    metrics[0].metric("Total records", len(records))
    metrics[1].metric("Submitted", len(submitted))
    metrics[2].metric("Emoji rate", f"{(emoji_count / max(1, len(records))) * 100:.0f}%")
    metrics[3].metric("Styles available", len(COMMENT_STYLES))
    st.info(style_recommendation(records))

    if records:
        style_counts = Counter(str(record.get("style", "unknown")) for record in records)
        research_counts = Counter(str(record.get("research_source", "chapter-only") or "chapter-only") for record in records)
        keyword_counts = Counter(str(record.get("keyword", "")).lower() for record in records if str(record.get("keyword", "")).strip())
        chart_cols = st.columns(3)
        chart_cols[0].bar_chart([{"style": style, "count": count} for style, count in style_counts.items()], x="style", y="count")
        chart_cols[1].bar_chart([{"source": source, "count": count} for source, count in research_counts.items()], x="source", y="count")
        chart_cols[2].bar_chart([{"keyword": keyword, "count": count} for keyword, count in keyword_counts.most_common(10)], x="keyword", y="count")

        recent_rows = [
            {
                "chapter": record.get("chapter_index"),
                "style": record.get("style"),
                "keyword": record.get("keyword"),
                "emoji": "yes" if record.get("emoji_used") else "no",
                "reflection": "yes" if record.get("reflection_used") else "no",
                "research": record.get("research_source") or "chapter-only",
                "comment": record.get("comment"),
            }
            for record in records[-30:]
        ]
        st.dataframe(recent_rows, use_container_width=True, hide_index=True)
    else:
        render_panel("No History Yet", f"History will appear after a run writes records to {HISTORY_FILE.name}.")

elif page == "Settings":
    render_page_header(
        "Settings",
        "Reference paths, available comment styles, and research endpoint behavior.",
        ["Configuration reference", "No run starts here"],
    )
    path_cols = st.columns(2)
    with path_cols[0]:
        st.markdown("#### Files")
        st.code(f"App folder: {APP_DIR}", language="text")
        st.code(f"Bot script: {BOT_PATH}", language="text")
        st.code(f"History file: {HISTORY_FILE}", language="text")
        st.code(f"Read marker file: {READ_MARKER_FILE}", language="text")
        st.code(f"Research file: {RESEARCH_FILE}", language="text")
    with path_cols[1]:
        st.markdown("#### Comment Styles")
        style_cols = st.columns(2)
        for index, style in enumerate(COMMENT_STYLES):
            style_cols[index % 2].code(style, language="text")

elif page == "Help":
    render_page_header(
        "Help",
        "Plain-language explanation of buttons, fields, research endpoints, and the recommended workflow.",
        ["Button guide", "Endpoint guide", "Workflow"],
    )
    st.markdown("#### Buttons")
    render_button_guide()
    st.markdown("#### Fields")
    st.markdown(
        """
        | Field | What it controls |
        |---|---|
        | `Starting chapter or series URL` | The first URL the bot opens. It can be a chapter page, reader page, or a series page if chapter links are detectable. |
        | `Maximum chapters` | Hard stop for how many chapter comment sections the bot processes. |
        | `Chapter routing` | `infinite-scroll` stays on one long reader page and finds the next lower comment form. `next-link` requires a next button/link. `auto` tries next-link first, then falls back to infinite scroll. |
        | `Click Say thanks and mark chapter as read` | Clicks the chapter `Say thanks` button after a successful comment and records the canonical chapter URL in `read_markers.jsonl`. Later runs skip locally marked chapters when possible. |
        | `Scroll speed` | Preset for page scrolling only. Typing speed is not affected. |
        | `Min/Max scroll px` | Manual pixel jump range for each scroll step. Higher values move faster. |
        | `Min/Max pause sec` | Manual pause range between scroll steps. Lower values move faster. |
        | `Local research file` | Optional JSON file used to enrich generated comments with staging-safe notes. |
        | `Research endpoint` | Optional local/private HTTP service called as `?q=<chapter title and keywords>`. |
        """
    )
    st.markdown("#### Manual Chapter Selection")
    st.markdown(
        """
        Use `Open Browser / Pick Chapter` when the pasted URL is a series page or when you want to choose a specific chapter yourself.
        The bot pauses until you click `Start Comments`, and it starts from the page currently open in Chrome.
        """
    )
    st.markdown("#### Research Endpoint")
    st.markdown(
        """
        You do not need a research endpoint. Leave it blank unless you run your own local service.

        If configured, the bot calls:

        ```text
        http://127.0.0.1:8000/research?q=Chapter Title keyword1 keyword2
        ```

        It may return plain text, or JSON:

        ```json
        {
          "summary": "A short context note for this chapter."
        }
        ```

        JSON fields `text` and `context` are also accepted.
        """
    )

if running:
    time.sleep(2)
    st.rerun()
