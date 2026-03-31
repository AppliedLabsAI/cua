#!/bin/bash
# Sandbox entrypoint — starts the desktop environment, status API, and agent loop.
#
# Environment variables (set by Modal sandbox via env=):
#   DISPLAY_NUM     — X display number (default: 99)
#   WIDTH           — display width in pixels (default: 1920)
#   HEIGHT          — display height in pixels (default: 1080)
#   DIRECTIVE       — natural language task for the agent
#   MODEL           — Claude model ID
#   MAX_STEPS       — maximum agent loop iterations
#   THINKING       — thinking effort level (minimal/low/medium/high/xhigh)
#   START_URL       — optional URL to open on browser launch
#   PROXY_URL       — optional residential proxy URL
#   CREDENTIALS_JSON  — optional JSON-encoded credentials dict
#   PROFILE         — agent profile name (default, research, form_filling)

set -uo pipefail

# Defaults
DISPLAY_NUM="${DISPLAY_NUM:-99}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
export DISPLAY=":${DISPLAY_NUM}"
XVFB_PID=""
OPENBOX_PID=""
TINT2_PID=""
AGENT_PID=""

log() { echo "[entrypoint] $(date -u +%H:%M:%S) $*" >&2; }
die() {
	log "FATAL: $*"
	exit 1
}
cleanup() {
	log "Received termination signal, cleaning up background processes"
	if [ -n "${AGENT_PID:-}" ]; then
		kill -TERM "$AGENT_PID" 2>/dev/null || true
		wait "$AGENT_PID" 2>/dev/null || true
	fi
	for pid in "$TINT2_PID" "$OPENBOX_PID" "$XVFB_PID"; do
		if [ -n "${pid:-}" ]; then
			kill "$pid" 2>/dev/null || true
		fi
	done
	exit 1
}
trap cleanup SIGTERM SIGINT

# ---------------------------------------------------------------------------
# 1. Virtual framebuffer
# ---------------------------------------------------------------------------
log "Starting Xvfb on :${DISPLAY_NUM} at ${WIDTH}x${HEIGHT}"
Xvfb ":${DISPLAY_NUM}" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac &
XVFB_PID=$!
sleep 1
kill -0 "$XVFB_PID" 2>/dev/null || die "Xvfb failed to start"

# ---------------------------------------------------------------------------
# 2. Window manager + panel
# ---------------------------------------------------------------------------
log "Starting openbox + tint2"
openbox &
OPENBOX_PID=$!
tint2 &
TINT2_PID=$!
sleep 0.5

# ---------------------------------------------------------------------------
# 3. Agent loop (also starts the status API in-process on port 8090)
# ---------------------------------------------------------------------------
if ! cd /opt/cua; then
	die "Failed to change directory to /opt/cua"
fi
log "Starting agent loop (status API on port 8090)"
log "  Directive: ${DIRECTIVE:0:100}"
log "  Model: ${MODEL:-claude-sonnet-4-6}"
log "  Max steps: ${MAX_STEPS:-50}"

python3 -m agent.main &
AGENT_PID=$!

# Wait for the agent loop to finish
wait "$AGENT_PID"
AGENT_EXIT=$?

if [ "$AGENT_EXIT" -eq 0 ]; then
	log "Agent completed successfully"
else
	log "Agent exited with code $AGENT_EXIT"
fi

# ---------------------------------------------------------------------------
# 5. Keep sandbox alive briefly for recording upload
# ---------------------------------------------------------------------------
log "Keeping sandbox alive for 10s (recording upload)"
sleep 10

log "Shutting down"
