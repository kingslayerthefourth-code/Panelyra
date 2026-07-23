"""Portable project, data, output, and legacy compatibility paths."""

from __future__ import annotations

import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
RUNS_DIR = PROJECT_ROOT / "runs"
UPLOADS_DIR = PROJECT_ROOT / "uploads"
DOWNLOADS_DIR = PROJECT_ROOT / "manhwa"

COMMENT_HISTORY_FILE = PROJECT_ROOT / "comment_history.jsonl"
READ_MARKERS_FILE = PROJECT_ROOT / "read_markers.jsonl"
OWNER_REQUESTS_FILE = PROJECT_ROOT / "owner_requests.jsonl"
RESEARCH_CONTEXT_FILE = PROJECT_ROOT / "research_context.json"

PANELYRA_LOG_DIR = Path(tempfile.gettempdir()) / "panelyra_logs"
PANELYRA_CACHE_DIR = Path(tempfile.gettempdir()) / "panelyra_pycache"
LEGACY_LOG_DIR = Path(tempfile.gettempdir()) / "asura_automation_logs"
LEGACY_CACHE_DIR = Path(tempfile.gettempdir()) / "asura_automation_pycache"

LEGACY_DOWNLOAD_DIR = PROJECT_ROOT / "ocr_downloads"
LEGACY_EXPORT_DIR = PROJECT_ROOT / "AsuraAutomation_GitHub"
PANELYRA_EXPORT_DIR = PROJECT_ROOT / "Panelyra_GitHub"


def ensure_runtime_directories() -> None:
    for path in (
        CONFIG_DIR,
        DATA_DIR,
        OUTPUTS_DIR,
        RUNS_DIR,
        UPLOADS_DIR,
        DOWNLOADS_DIR,
        PANELYRA_LOG_DIR,
        PANELYRA_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
