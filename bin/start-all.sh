#!/usr/bin/env bash
#
# start-all.sh — bring up the full Transcript Intelligence dev environment.
#
# Starts (in parallel):
#   - Public API + dashboard    :8000   (api.main, /api/v1/* + dashboard at /)
#   - Admin panel               :8001   (api.admin_app, /admin + /api/v1/admin)
#   - Jupyter Lab               :8888   (narrative notebook)
#   - Static docs HTTP server   :8765   (HTML documentation)
#
# The admin panel runs as a SEPARATE process on its own port so the platform
# can route admin traffic through a private listener (see ADR 0014 §"Control
# plane vs. data plane" and deploy/k8s/gateway.yaml). Both processes share
# the admin DB; settings changes propagate via LISTEN/NOTIFY (Postgres) or
# the 5s TTL cache fallback (SQLite).
#
# Pre-flight: ensures tests pass and validation succeeds before launch.
#
# Usage:
#   ./bin/start-all.sh                                    # run with defaults
#   SKIP_PREFLIGHT=1 ./bin/start-all.sh                   # skip tests + validate
#   API_PORT=9000 ADMIN_PORT=9001 ./bin/start-all.sh      # custom ports
#
# Press Ctrl+C to gracefully stop everything.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config (override via env)
# ---------------------------------------------------------------------------
API_PORT="${API_PORT:-8000}"
ADMIN_PORT="${ADMIN_PORT:-8001}"
JUPYTER_PORT="${JUPYTER_PORT:-8888}"
DOCS_PORT="${DOCS_PORT:-8765}"
SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-0}"
LOG_DIR="${LOG_DIR:-.run-logs}"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p "$LOG_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log()  { printf "${BLUE}▶${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}✔${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()  { printf "${RED}✘${NC} %s\n" "$*" >&2; }

PIDS=()
SHUTTING_DOWN=0

# Recursively kill a process and all its descendants (uvicorn workers,
# jupyter kernels, etc.). pgrep is portable across macOS/Linux.
kill_tree() {
    local pid=$1 sig=${2:-TERM}
    [[ -z "$pid" ]] && return
    # Kill children first so signals don't get reparented to init
    if command -v pgrep >/dev/null 2>&1; then
        for child in $(pgrep -P "$pid" 2>/dev/null); do
            kill_tree "$child" "$sig"
        done
    fi
    kill -"$sig" "$pid" 2>/dev/null || true
}

cleanup() {
    # Idempotent — trap can fire on both INT/TERM and EXIT
    [[ "$SHUTTING_DOWN" == "1" ]] && return
    SHUTTING_DOWN=1

    echo
    log "Stopping services…"

    # 1. Polite SIGTERM to each tracked process tree
    for pid in "${PIDS[@]:-}"; do
        [[ -n "${pid:-}" ]] && kill_tree "$pid" TERM
    done

    sleep 1

    # 2. Force-kill anything still alive
    for pid in "${PIDS[@]:-}"; do
        [[ -n "${pid:-}" ]] && kill_tree "$pid" KILL
    done

    ok "All services stopped"
    exit 0
}
trap cleanup INT TERM
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
port_in_use() {
    lsof -iTCP:"$1" -sTCP:LISTEN -P >/dev/null 2>&1
}

wait_for_http() {
    local url=$1 name=$2 max_attempts=${3:-30}
    for i in $(seq 1 "$max_attempts"); do
        if curl -sf -o /dev/null "$url" 2>/dev/null; then
            ok "$name is ready"
            return 0
        fi
        sleep 0.5
    done
    err "$name failed to become ready in $((max_attempts / 2))s"
    return 1
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "Required command not found: $1"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
printf "\n${BOLD}═══ Transcript Intelligence: dev launcher ═══${NC}\n\n"

require_cmd python || exit 1
require_cmd uvicorn || { err "uvicorn missing — run: pip install -e '.[dev]'"; exit 1; }
require_cmd jupyter || warn "jupyter missing — Jupyter Lab will be skipped"
require_cmd lsof    || warn "lsof not available — port-conflict checks disabled"
require_cmd curl    || { err "curl required for readiness checks"; exit 1; }

# Port-conflict check
for port_var in API_PORT ADMIN_PORT JUPYTER_PORT DOCS_PORT; do
    port=${!port_var}
    if port_in_use "$port" 2>/dev/null; then
        err "$port_var ($port) is already in use. Stop the conflicting process or set $port_var=<other>."
        exit 1
    fi
done

if [[ "$SKIP_PREFLIGHT" != "1" ]]; then
    log "Pre-flight: running tests…"
    if python -m pytest tests/ -q --no-cov >"$LOG_DIR/pytest.log" 2>&1; then
        ok "Tests passed"
    else
        err "Tests failed — see $LOG_DIR/pytest.log"
        exit 1
    fi

    log "Pre-flight: semantic validation…"
    if python validate.py >"$LOG_DIR/validate.log" 2>&1; then
        fails=$(grep -c "FAIL" "$LOG_DIR/validate.log" || true)
        if [[ "$fails" -gt 0 ]]; then
            err "Validation reported $fails FAIL(s) — see $LOG_DIR/validate.log"
            exit 1
        fi
        # Pull the "Summary: N pass · N warn · N fail" line for display
        summary_line=$(grep -E "^\s*Summary:" "$LOG_DIR/validate.log" | tr -s ' ' | sed 's/^ //')
        ok "Validation OK${summary_line:+ — $summary_line}"
    else
        err "Validation failed — see $LOG_DIR/validate.log"
        exit 1
    fi
else
    warn "Pre-flight skipped (SKIP_PREFLIGHT=1)"
fi

# ---------------------------------------------------------------------------
# Refresh static artifacts (in parallel)
# ---------------------------------------------------------------------------
log "Refreshing pipeline outputs and HTML docs (parallel)…"

python run_analysis.py >"$LOG_DIR/run_analysis.log" 2>&1 &
PID_BATCH=$!
python build_docs.py >"$LOG_DIR/build_docs.log" 2>&1 &
PID_DOCS=$!

wait "$PID_BATCH" || { err "run_analysis.py failed — see $LOG_DIR/run_analysis.log"; exit 1; }
wait "$PID_DOCS"  || { err "build_docs.py failed — see $LOG_DIR/build_docs.log"; exit 1; }
ok "Outputs refreshed (output/, docs/html/)"

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
log "Starting public API on :$API_PORT …"
uvicorn api.main:app \
    --host 127.0.0.1 --port "$API_PORT" \
    --log-level info \
    >"$LOG_DIR/api.log" 2>&1 &
PIDS+=($!)
wait_for_http "http://127.0.0.1:$API_PORT/api/live" "Public API"

log "Starting admin panel on :$ADMIN_PORT …"
uvicorn api.admin_app:app \
    --host 127.0.0.1 --port "$ADMIN_PORT" \
    --log-level info \
    >"$LOG_DIR/admin.log" 2>&1 &
PIDS+=($!)
wait_for_http "http://127.0.0.1:$ADMIN_PORT/api/live" "Admin panel"

if command -v jupyter >/dev/null 2>&1; then
    log "Starting Jupyter Lab on :$JUPYTER_PORT …"
    # Bind to 127.0.0.1 only; disable token for local dev convenience
    jupyter lab \
        --ip=127.0.0.1 --port="$JUPYTER_PORT" \
        --no-browser \
        --ServerApp.token='' --ServerApp.password='' \
        --ServerApp.allow_origin='http://127.0.0.1:*' \
        --notebook-dir="$PROJECT_ROOT" \
        >"$LOG_DIR/jupyter.log" 2>&1 &
    PIDS+=($!)
    wait_for_http "http://127.0.0.1:$JUPYTER_PORT" "Jupyter Lab"
fi

log "Starting docs HTTP server on :$DOCS_PORT …"
(cd docs/html && python -m http.server "$DOCS_PORT" --bind 127.0.0.1) \
    >"$LOG_DIR/docs.log" 2>&1 &
PIDS+=($!)
wait_for_http "http://127.0.0.1:$DOCS_PORT/index.html" "Docs server"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
printf "${GREEN}${BOLD}╭──────────────────────────────────────────────────────────╮${NC}\n"
printf "${GREEN}${BOLD}│  All services running                                    │${NC}\n"
printf "${GREEN}${BOLD}╰──────────────────────────────────────────────────────────╯${NC}\n"
echo
printf "  ${BOLD}Dashboard${NC}      ${BLUE}http://127.0.0.1:%s${NC}\n" "$API_PORT"
printf "  ${BOLD}OpenAPI docs${NC}   ${BLUE}http://127.0.0.1:%s/docs${NC}\n" "$API_PORT"
printf "  ${BOLD}Admin panel${NC}    ${BLUE}http://127.0.0.1:%s/${NC}   ${DIM}(separate process)${NC}\n" "$ADMIN_PORT"
printf "  ${BOLD}Jupyter${NC}        ${BLUE}http://127.0.0.1:%s/lab/tree/transcript_intelligence.ipynb${NC}\n" "$JUPYTER_PORT"
printf "  ${BOLD}HTML docs${NC}      ${BLUE}http://127.0.0.1:%s${NC}\n" "$DOCS_PORT"
echo
printf "  ${DIM}Logs:           ./%s/${NC}\n" "$LOG_DIR"
printf "  ${DIM}Press Ctrl+C to stop all services${NC}\n"
echo

# Block until any child exits OR we get a signal
wait
