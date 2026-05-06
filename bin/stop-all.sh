#!/usr/bin/env bash
#
# stop-all.sh — kill any lingering Transcript Intelligence services
#                started outside the start-all.sh trap (e.g., orphaned).
#
# Matches by command pattern, not PID file, so it's safe to run anytime.

set -euo pipefail

API_PORT="${API_PORT:-8000}"
JUPYTER_PORT="${JUPYTER_PORT:-8888}"
DOCS_PORT="${DOCS_PORT:-8765}"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

kill_port() {
    local port=$1 name=$2
    local pids
    pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 0.5
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
        printf "${GREEN}✔${NC} stopped %s on :%s (pid %s)\n" "$name" "$port" "$pids"
    else
        printf "${YELLOW}–${NC} %s on :%s not running\n" "$name" "$port"
    fi
}

if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof not available; cannot identify services by port" >&2
    exit 1
fi

kill_port "$API_PORT"     "FastAPI"
kill_port "$JUPYTER_PORT" "Jupyter Lab"
kill_port "$DOCS_PORT"    "Docs server"
