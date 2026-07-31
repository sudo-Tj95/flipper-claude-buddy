#!/usr/bin/env python3
"""Detect and register the current Claude runner target with the bridge."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys


TERM_PROGRAM_APP_NAMES = {
    # macOS terminals
    "Apple_Terminal": "Terminal",
    "Terminal": "Terminal",
    "Terminal.app": "Terminal",
    "iTerm.app": "iTerm",
    "iTerm2": "iTerm",
    # cross-platform
    "vscode": "Visual Studio Code",
    "WarpTerminal": "Warp",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "Ghostty": "Ghostty",
    # Linux terminals
    "gnome-terminal": "GNOME Terminal",
    "konsole": "Konsole",
    "xterm": "XTerm",
    "alacritty": "Alacritty",
    "kitty": "kitty",
    "tilix": "Tilix",
    "xfce4-terminal": "Xfce Terminal",
}


# Marker substrings identifying a VS Code extension host in the parent process
# chain. When Claude Code runs as the VS Code *extension* (side bar or tab) there
# is no controlling tty and TERM_PROGRAM is unset, so walking up to the Electron
# process that spawned us is the only way to recognise the host.
VSCODE_PARENT_MARKERS = (
    "Visual Studio Code.app",
    "Code Helper (Plugin)",
    "Code Helper",
    "/usr/share/code/",
    "/opt/visual-studio-code/",
    "\\Microsoft VS Code\\",
)

# macOS application name to activate for the extension host.
VSCODE_APP_NAME = "Visual Studio Code"

# Default hotkey the bridge presses to move the caret into the Claude Code
# input before typing. It must be bound to `claude-vscode.focus` in VS Code's
# keybindings.json, guarded by
# `"when": "activeWebviewPanelId == 'claudeVSCodePanel'"` — without that guard
# the command opens the last Claude session in a tab, which may not be the
# session this Flipper is driving. See FORK.md ("VS Code extension support").
# Deliberately NOT cmd+escape: VS Code binds that to focus OR blur depending on
# where focus already is, so pressing it blind would sometimes dismiss the input.
DEFAULT_VSCODE_FOCUS_HOTKEY = "cmd+ctrl+alt+j"


def _normalize_tty(value: str) -> str:
    value = (value or "").strip()
    if not value or value == "??":
        return ""
    if value.startswith("/dev/"):
        return value
    return f"/dev/{value}"


def detect_tty() -> str:
    for fd in (0, 1, 2):
        try:
            return _normalize_tty(os.ttyname(fd))
        except OSError:
            pass

    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            ppid = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            tty = subprocess.check_output(
                ["ps", "-o", "tty=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            break

        normalized_tty = _normalize_tty(tty)
        if normalized_tty:
            return normalized_tty

        try:
            pid = int(ppid)
        except ValueError:
            break

    return ""


def detect_vscode_extension_host() -> bool:
    """True if we were spawned by the VS Code extension host (not a terminal).

    Only meaningful when TERM_PROGRAM is empty: VS Code's *integrated terminal*
    sets TERM_PROGRAM=vscode and has a real tty, and is handled by the normal
    terminal path. The extension host has neither, and its keystroke target is
    a webview rather than a TUI, so it needs different focus handling.
    """
    pid = os.getpid()
    seen: set[int] = set()
    # Depth cap: shell -> claude binary -> Code Helper (Plugin) -> Code is 4
    # levels; allow headroom for wrappers without risking a long ps walk.
    for _ in range(12):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            break
        if not out:
            break
        ppid_str, _, command = out.partition(" ")
        if any(marker in command for marker in VSCODE_PARENT_MARKERS):
            return True
        try:
            pid = int(ppid_str)
        except ValueError:
            break
    return False


def build_target() -> dict[str, str]:
    term_program = (os.environ.get("TERM_PROGRAM") or "").strip()
    tty = detect_tty()
    app_name = TERM_PROGRAM_APP_NAMES.get(term_program, term_program)
    focus_mode = ""
    focus_hotkey = ""

    # VS Code extension host: no TERM_PROGRAM, no tty, Electron parent.
    if not term_program and not tty and detect_vscode_extension_host():
        app_name = VSCODE_APP_NAME
        focus_mode = "vscode_webview"
        focus_hotkey = (
            os.environ.get("FLIPPER_VSCODE_FOCUS_HOTKEY") or DEFAULT_VSCODE_FOCUS_HOTKEY
        ).strip()

    target = {
        "app_name": app_name,
        "term_program": term_program,
        "term_session_id": (os.environ.get("TERM_SESSION_ID") or "").strip(),
        "iterm_session_id": (os.environ.get("ITERM_SESSION_ID") or "").strip(),
        "tty": tty,
        # X11 window ID — set by VTE-based terminals (gnome-terminal, kitty, etc.)
        # Used by XdotoolInputBackend on Linux to focus the correct window.
        "window_id": (os.environ.get("WINDOWID") or "").strip(),
        # Fork addition: how to put the caret where typing should land.
        # "" = terminal (activate app / select tab by tty).
        # "vscode_webview" = activate VS Code, then press focus_hotkey.
        "focus_mode": focus_mode,
        "focus_hotkey": focus_hotkey,
    }
    material = json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
    target["session_key"] = hashlib.sha1(material).hexdigest()[:16]
    return target


def send_action(socket_path: str, action: str, target: dict[str, str]) -> int:
    payload = {"action": action, **target}
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(json.dumps(payload).encode())
        client.shutdown(socket.SHUT_WR)
        client.recv(65536)
        client.close()
    except Exception:
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"register_target", "release_target"}:
        print(
            "usage: session-target.py <register_target|release_target> <socket>",
            file=sys.stderr,
        )
        return 2

    socket_path = argv[2]
    if not os.path.exists(socket_path):
        return 0

    target = build_target()
    if not any(
        (
            target["app_name"],
            target["tty"],
            target["term_session_id"],
            target["iterm_session_id"],
        )
    ):
        return 0

    return send_action(socket_path, argv[1], target)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
