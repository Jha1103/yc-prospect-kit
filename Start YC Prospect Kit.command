#!/bin/bash
set -e
cd "$(dirname "$0")"
printf '\nYC Prospect Kit — Local dashboard\n\n'
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 is required. Install Python 3 and try again.'
  read -r -p 'Press Enter to close...' _
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo 'First launch: creating local Python environment.'
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import requests, bs4' >/dev/null 2>&1; then
  echo 'Installing dependencies (first launch only)...'
  .venv/bin/python -m pip install -r requirements.txt
fi
if .venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8766/api/health",timeout=1)' >/dev/null 2>&1; then
  echo 'The dashboard is already running; opening it in your browser.'
  open 'http://127.0.0.1:8766/'
  exit 0
fi
.venv/bin/python -m ycprospect.web
