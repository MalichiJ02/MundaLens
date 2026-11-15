#!/usr/bin/env bash
# Starts MundaLens and loads NASA_EARTHDATA_TOKEN from a private .env file if one exists.
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
[ -f .env ] && source .env
python -m uvicorn app:app --host 127.0.0.1 --port 8000
