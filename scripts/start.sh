#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
exec .venv/bin/python -m uvicorn jarvis.app:app --host 127.0.0.1 --port 8765 --no-proxy-headers --no-access-log
