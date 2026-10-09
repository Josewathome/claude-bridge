#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "venv missing - run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
fi

set -a
source .env
set +a

exec ./venv/bin/uvicorn gateway:app --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-8787}"
