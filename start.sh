#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -f .venv/bin/activate ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

source .venv/bin/activate

# Re-index on startup so the server always has fresh data.
python indexer.py

echo "Starting local-search on http://localhost:8000"
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
