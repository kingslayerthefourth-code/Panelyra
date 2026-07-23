"""Package entry point for the Panelyra Streamlit dashboard."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT_APP = Path(__file__).resolve().parents[1] / "app.py"
sys.path.insert(0, str(ROOT_APP.parent))
runpy.run_path(str(ROOT_APP), run_name="__main__")
