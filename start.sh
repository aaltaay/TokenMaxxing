#!/usr/bin/env bash
# Launch the AI Usage Command Center (Electron UI + Python engine).
set -euo pipefail
cd "$(dirname "$0")/app"

if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js is required. Install it from https://nodejs.org and run this again." >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "First run: installing Electron..."
  npm install --no-audit --no-fund
fi

exec npm start
