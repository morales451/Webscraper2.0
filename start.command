#!/bin/bash
# Prospector launcher for macOS / Linux: double-click (macOS) or run ./start.command
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Setting up Prospector for the first time. This takes a few minutes..."
  python3 -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e . || { echo "Setup failed. Delete the .venv folder and try again."; exit 1; }
  .venv/bin/python -m playwright install chromium || exit 1
fi

exec .venv/bin/python -m prospector serve
