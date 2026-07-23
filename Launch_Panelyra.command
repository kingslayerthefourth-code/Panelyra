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
export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/panelyra_pycache"
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
PANELYRA_PORT="${PANELYRA_PORT:-8517}"
PANELYRA_OPEN_BROWSER="${PANELYRA_OPEN_BROWSER:-true}"

if [ -x ".venv/bin/python" ]; then
  PYTHON_RUNNER=".venv/bin/python"
else
  PYTHON_RUNNER="python3"
fi

if ! "$PYTHON_RUNNER" -c "import streamlit" >/dev/null 2>&1; then
  echo "Streamlit was not found."
  echo "Install project dependencies with: python3 -m pip install -r requirements.txt"
  echo "Press any key to close this window."
  read -n 1 -s
  exit 1
fi

echo "Launching Panelyra Control"
while lsof -nP -iTCP:"$PANELYRA_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PANELYRA_PORT=$((PANELYRA_PORT + 1))
done

DASHBOARD_URL="http://127.0.0.1:$PANELYRA_PORT"
echo "Dashboard: $DASHBOARD_URL"
echo "Project: $APP_DIR"
echo

"$PYTHON_RUNNER" -m streamlit run panelyra/app.py \
  --server.port "$PANELYRA_PORT" \
  --server.headless false \
  --browser.gatherUsageStats false &
SERVER_PID=$!

if [ "$PANELYRA_OPEN_BROWSER" = "true" ]; then
  for _ in {1..30}; do
    if curl -fsS "$DASHBOARD_URL/_stcore/health" >/dev/null 2>&1; then
      open "$DASHBOARD_URL"
      break
    fi
    sleep 1
  done
fi

wait "$SERVER_PID"
