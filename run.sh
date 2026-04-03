#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then echo "No .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; fi
if [[ ! -f questions.json ]]; then echo "Missing questions.json. Run: .venv/bin/python extract_questions.py"; exit 1; fi
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 "$@"
