#!/bin/bash

# SessionEnd hook: notify Flipper of disconnect, then stop the bridge
# when the last session ends (reference-counted).

SOCKET="/tmp/claude-flipper-bridge.sock"
PIDFILE="/tmp/claude-flipper-bridge.pid"
# One marker file per live session — see the "Session tracking" comment in
# on-session-start.sh for why this replaced upstream's single refcount file.
SESSIONS_DIR="/tmp/claude-flipper-bridge.sessions"

# Read hook payload from stdin and extract the session end reason.
PAYLOAD=$(cat)
REASON=$(echo "$PAYLOAD" | python3 -c '
import json, sys
REASONS = {
    "clear": "Cleared",
    "resume": "Switched session",
    "logout": "Logged out",
    "prompt_input_exit": "User exited",
    "bypass_permissions_disabled": "Bypass perms off",
    "other": "Disconnected",
}
try:
    data = json.load(sys.stdin)
    raw = data.get("reason") or ""
    print(REASONS.get(raw, raw or "Disconnected")[:21])
except Exception:
    print("Disconnected")
' 2>/dev/null)

# Remove this session's marker, then count what remains. Must use the same
# id derivation as on-session-start.sh, including the "legacy" fallback.
SESSION_ID=$(echo "$PAYLOAD" | python3 -c '
import json, re, sys
try:
    sid = json.load(sys.stdin).get("session_id") or ""
except Exception:
    sid = ""
sid = re.sub(r"[^A-Za-z0-9_.-]", "", str(sid))[:64]
print(sid or "legacy")
' 2>/dev/null || echo "legacy")

rm -f "$SESSIONS_DIR/$SESSION_ID"
COUNT=$(ls -1 "$SESSIONS_DIR" 2>/dev/null | wc -l | tr -d ' ')

if [ -S "$SOCKET" ]; then
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session-target.py" release_target "$SOCKET" >/dev/null 2>&1 || true
    echo '{"action":"claude_disconnect"}' \
        | nc -U "$SOCKET" 2>/dev/null || true
fi

# Only stop bridge when last session ends
if [ "$COUNT" -le 0 ]; then
    if [ -S "$SOCKET" ]; then
        echo "{\"action\":\"notify\",\"sound\":\"session_end\",\"vibro\":true,\"text\":\"Session End\",\"subtext\":\"$REASON\"}" \
            | nc -U "$SOCKET" 2>/dev/null || true
        # Give bridge time to deliver the message to Flipper
        sleep 0.5
    fi

    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
        fi
        rm -f "$PIDFILE"
    fi
    rmdir "$SESSIONS_DIR" 2>/dev/null || true
fi

exit 0
