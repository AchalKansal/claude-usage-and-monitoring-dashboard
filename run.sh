#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Install dependencies if needed
if ! python3 -c "import flask, watchdog" 2>/dev/null; then
  echo "[setup] Installing dependencies…"
  pip3 install -r requirements.txt -q
fi

echo "[claude-monitor] Starting on http://localhost:7845"
python3 app.py
