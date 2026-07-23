#!/bin/bash

set -e

PANELYRA_DIR="/Users/firdousmaliyekkal/Documents/AsuraAutomations"
PANELYRA_LAUNCHER="$PANELYRA_DIR/Launch_Panelyra.command"

if [ ! -x "$PANELYRA_LAUNCHER" ]; then
  echo "Panelyra launcher was not found at:"
  echo "$PANELYRA_LAUNCHER"
  echo
  echo "Press any key to close this window."
  read -n 1 -s
  exit 1
fi

exec "$PANELYRA_LAUNCHER"
