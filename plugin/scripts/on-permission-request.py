#!/usr/bin/env python3
"""PermissionRequest hook: shows permission request on Flipper, waits for user decision."""

import json
import os
import re
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


# Must match protocol.PERM_DETAIL_MAX / PERM_TOOL_MAX in the bridge. The hook
# runs as a standalone script with no access to the bridge's venv, so the
# limits are duplicated rather than imported.
DETAIL_MAX = 63
TOOL_MAX = 21


# What to show for Bash prompts: "description" (default), "command", or "both".
#
# Description wins by default because commands are usually front-loaded with
# boilerplate — most agent-issued commands begin with a long `cd "/path" &&`,
# which is exactly the part that survives truncation and tells you nothing:
#
#   |cd "/Users/tonyjoy/D|
#   |ocuments/Claude     |
#   |Projects/flipper-   |
#
# "command" mode is available for anyone who wants ground truth over summary —
# a description is prose written alongside the call and can describe something
# other than what runs. Both modes strip the boilerplate prefix first.
DETAIL_MODE = (
    os.environ.get("CLAUDE_PLUGIN_OPTION_permissionDetail")
    or os.environ.get("FLIPPER_PERM_DETAIL")
    or "description"
).strip().lower()

# Leading `cd <path> &&`, repeated, plus leading VAR=value assignments.
_NOISE_RE = re.compile(
    r'^\s*(?:cd\s+(?:"[^"]*"|\'[^\']*\'|[^\s&;|]+)\s*&&\s*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+'
)


def _strip_noise(cmd: str) -> str:
    """Drop `cd …&&` / env-assignment prefixes that would eat the whole display."""
    stripped = _NOISE_RE.sub("", cmd, count=1)
    # If stripping left nothing meaningful, the prefix *was* the command.
    return stripped if stripped.strip() else cmd


def _fit(text: str) -> str:
    """Make *text* safe for the Flipper's `char detail[64]` and its font.

    The device draws single-byte glyphs and copies into a fixed 64-byte buffer,
    so anything non-ASCII would both render as garbage and risk being cut
    mid-sequence. Collapse whitespace, drop non-printable and non-ASCII
    characters, then cap by BYTES rather than characters.
    """
    text = " ".join(str(text).split())
    text = "".join(c for c in text if 0x20 <= ord(c) < 0x7F)
    return text.encode("ascii", "ignore")[:DETAIL_MAX].decode("ascii")


def extract_detail(tool_name: str, tool_input: dict) -> str:
    """Extract a detail string from the tool input, for display on the Flipper.

    Fits the device's `char detail[64]`, wrapped to three lines. Upstream capped
    everything at 21 characters, which is why a Bash prompt read as e.g.
    "Check whether a permi" — enough to identify nothing.
    """
    # Special handling for mcp__atlassian__searchJiraIssuesUsingJql and similar
    if "__" in tool_name:
        parts = tool_name.split("__")
        if len(parts) >= 3:
            # e.g. mcp__atlassian__searchJiraIssuesUsingJql
            return _fit(parts[-1])
    if tool_name == "Bash":
        desc = str(tool_input.get("description", "")).strip()
        cmd = _strip_noise(str(tool_input.get("command", ""))).strip()
        if DETAIL_MODE == "command":
            return _fit(cmd or desc)
        if DETAIL_MODE == "both" and desc and cmd:
            # Description first so it survives truncation; command fills the rest.
            return _fit(f"{desc}: {cmd}")
        return _fit(desc or cmd)
    if tool_name in ("Edit", "Write", "Read"):
        path = str(tool_input.get("file_path", ""))
        if not path:
            return ""
        if len(path) <= DETAIL_MAX:
            return _fit(path)
        # Keep the tail: filename plus as much parent as fits identifies the
        # target far better than a bare basename. Realign to a path separator
        # so it does not start mid-component, and mark the elision in ASCII
        # ("..." not "…" — the device has no UTF-8 font).
        tail = path[-(DETAIL_MAX - 3):]
        cut = tail.find("/")
        if 0 <= cut < 20:
            tail = tail[cut:]
        return _fit("..." + tail)
    if tool_name in ("WebFetch", "WebSearch"):
        val = str(tool_input.get("url") or tool_input.get("query", ""))
        for prefix in ("https://", "http://"):
            if val.startswith(prefix):
                val = val[len(prefix):]
                break
        return _fit(val)
    if tool_name == "Agent":
        return _fit(tool_input.get("description", ""))
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
    # writes a persistent permission rule into settings.json. Even with this
    # fork's wider detail (63 chars over three wrapped lines, up from upstream's
    # 21), a 128x64 screen is not the place to grant a standing rule — a one-off
    # "yes" should not silently become "never ask me about this again".
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
