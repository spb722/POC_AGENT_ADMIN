#!/usr/bin/env bash
# Kill any running `uvicorn main:app` and bring it back up the same way it
# was originally started: nohup'd, host 0.0.0.0, port 8086, auto-reload,
# logging to uvicorn.log in this directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8086
HOST=0.0.0.0

echo "Stopping any running uvicorn main:app ..."
pkill -f "uvicorn main:app" 2>/dev/null && sleep 1 || echo "  (nothing was running)"

echo "Starting uvicorn main:app on ${HOST}:${PORT} ..."
source .venv/bin/activate
nohup uvicorn main:app --reload --host "$HOST" --port "$PORT" > uvicorn.log 2>&1 &
PID=$!
disown

sleep 1
echo "Started with PID ${PID}. Logs: $(pwd)/uvicorn.log"
