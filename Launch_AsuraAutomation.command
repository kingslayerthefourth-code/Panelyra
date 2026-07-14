#!/bin/bash

set -e

LAUNCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$LAUNCHER_DIR"

if [ ! -f "$APP_DIR/app.py" ]; then
  echo "Could not find app.py next to this launcher."
  echo "Press any key to close this window."
  read -n 1 -s
  exit 1
fi

cd "$APP_DIR"

if [ -d ".venv" ]; then
  source ".venv/bin/activate"
fi

if ! command -v streamlit >/dev/null 2>&1; then
  echo "Streamlit was not found."
  echo "Install project dependencies with: python3 -m pip install -r requirements.txt"
  echo "Press any key to close this window."
  read -n 1 -s
  exit 1
fi

streamlit run app.py
