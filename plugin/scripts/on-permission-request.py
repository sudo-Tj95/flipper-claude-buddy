#!/usr/bin/env python3
"""PermissionRequest hook: shows permission request on Flipper, waits for user decision."""

import json
import os
import socket
import sys

SOCKET_PATH = "/tmp/claude-flipper-bridge.sock"
TIMEOUT = 60  # seconds to wait for user decision on Flipper


def send_to_bridge(tool: str, detail: str) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect(SOCKET_PATH)
    msg = json.dumps({"action": "permission_request", "tool": tool, "detail": detail})
    s.sendall(msg.encode())
    s.shutdown(socket.SHUT_WR)
    resp = s.recv(4096)
    s.close()
    return json.loads(resp.decode())


def extract_detail(tool_name: str, tool_input: dict) -> str:
    """Extract a short detail string from the tool input."""
    # Special handling for mcp__atlassian__searchJiraIssuesUsingJql and similar
    if "__" in tool_name:
        parts = tool_name.split("__")
        if len(parts) >= 3:
            # e.g. mcp__atlassian__searchJiraIssuesUsingJql
            return parts[-1][:21]
    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        if desc:
            return desc[:21]
        cmd = tool_input.get("command", "")
        return cmd[:21] if cmd else ""
    if tool_name in ("Edit", "Write", "Read"):
        path = tool_input.get("file_path", "")
        return os.path.basename(path)[:21] if path else ""
    if tool_name in ("WebFetch", "WebSearch"):
        val = tool_input.get("url") or tool_input.get("query", "")
        for prefix in ("https://", "http://"):
            if val.startswith(prefix):
                val = val[len(prefix):]
                break
        return val[:21]
    if tool_name == "Agent":
        return tool_input.get("description", "")[:21]
    return ""


def main():
    if not os.path.exists(SOCKET_PATH):
        # Bridge not running — fall back to normal permission dialog
        sys.exit(1)

    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(1)


    tool_name_raw = hook_input.get("tool_name", "Unknown")
    tool_input = hook_input.get("tool_input", {})

    # For tool_name like mcp__atlassian__searchJiraIssuesUsingJql, display as mcp_atlassian
    if "__" in tool_name_raw:
        parts = tool_name_raw.split("__")
        if len(parts) >= 2:
            tool_name = f"{parts[0]}_{parts[1]}"
        else:
            tool_name = tool_name_raw
    else:
        tool_name = tool_name_raw

    detail = extract_detail(tool_name_raw, tool_input)

    try:
        result = send_to_bridge(tool_name, detail)
    except Exception:
        # Bridge error — fall back to normal permission dialog
        sys.exit(1)

    status = result.get("status")

    # Dismissed on Flipper — defer to Claude's normal permission dialog
    if status == "ask":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "ask"},
            }
        }))
        sys.exit(0)

    # Only act on explicit user decisions from Flipper
    if status != "ok":
        # no_flipper, timeout, busy, error — fall back to normal dialog
        sys.exit(1)

    allowed = result.get("allowed", False)

    # "Always allow" is deliberately not honoured in this fork.
    #
    # Upstream maps the Flipper's ALWAYS button onto `updatedPermissions`, which
    # writes a persistent permission rule into settings.json. The Flipper shows
    # at most 21 characters of the tool detail on a 128x64 screen, which is not
    # enough context to grant a standing rule — a one-off "yes" to a truncated
    # Bash description should not silently become "never ask me about this again".
    # The bridge already forces always=False (see daemon.py); dropping it here too
    # keeps the guarantee even if an older or spoofed bridge sends always=true.
    # ALWAYS therefore behaves exactly like a one-time ALLOW.
    if allowed:
        decision = {"behavior": "allow"}
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            }
        }
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "Denied on Flipper"},
            }
        }

    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
