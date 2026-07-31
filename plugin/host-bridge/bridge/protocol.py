"""JSON protocol for Flipper <-> Host Bridge communication."""

import json
import re
import time
import uuid

def make_id() -> str:
    return uuid.uuid4().hex[:8]


def encode(msg_type: str, data: dict | None = None) -> bytes:
    msg = {
        "v": 1,
        "t": msg_type,
        "d": data or {},
    }
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
        if isinstance(msg, dict) and "t" in msg:
            return msg
    except json.JSONDecodeError:
        pass
    return None


def notify_msg(sound: str, vibro: bool = True, text: str = "", subtext: str = "") -> bytes:
    d: dict = {"sound": sound, "vibro": vibro, "text": text}
    if subtext:
        d["sub"] = subtext[:21]
    return encode("notify", d)


def state_msg(claude_connected: bool = False) -> bytes:
    return encode("state", {"claude": claude_connected})


def status_msg(line1: str, line2: str = "") -> bytes:
    d: dict = {"line1": line1[:21]}
    if line2:
        d["line2"] = line2[:21]
    return encode("status", d)


def ping_msg(rssi: int | None = None) -> bytes:
    d: dict[str, int] = {}
    if rssi is not None:
        d["rssi"] = int(rssi)
    return encode("ping", d)


def menu_msg(items: list[str]) -> bytes:
    return encode("menu", {"items": "|".join(items)})


# The Flipper's permission view holds `char detail[64]` and wraps it to three
# lines (ui.h:88, ui.c:1311), and the wire format allows 64-byte fields
# (PROTOCOL_MAX_FIELD_LEN). Upstream truncated the detail to 21 characters here
# anyway — one line's worth — so a Bash approval showed the first 21 characters
# of its description and told you almost nothing about what would actually run.
# Removing the Once/Always toggle freed the row it occupied, taking the detail
# from two wrapped lines to three, so we now send what the device can display.
# `tool` stays at 21 because PermModel.tool is char[22].
PERM_TOOL_MAX = 21
PERM_DETAIL_MAX = 63

# Leading `cd <path> &&`, repeated, plus leading VAR=value assignments. Agent
# commands are front-loaded with these and they would otherwise consume the
# entire three-line display, which is why "description" is the default mode.
_NOISE_RE = re.compile(
    r'^\s*(?:cd\s+(?:"[^"]*"|\'[^\']*\'|[^\s&;|]+)\s*&&\s*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+'
)


def strip_command_noise(cmd: str) -> str:
    """Drop `cd …&&` / env-assignment prefixes from a shell command."""
    stripped = _NOISE_RE.sub("", cmd or "", count=1)
    # If stripping left nothing, the prefix *was* the command.
    return stripped if stripped.strip() else (cmd or "")


def fit_detail(text: str) -> str:
    """Make *text* safe for the Flipper's `char detail[64]` and its font.

    The device draws single-byte glyphs and copies into a fixed 64-byte buffer,
    so non-ASCII would both render as garbage and risk a cut mid-sequence.
    Collapse whitespace, drop non-printable/non-ASCII, cap by BYTES.
    """
    text = " ".join(str(text).split())
    text = "".join(c for c in text if 0x20 <= ord(c) < 0x7F)
    return text.encode("ascii", "ignore")[:PERM_DETAIL_MAX].decode("ascii")


def format_perm_detail(description: str, command: str, mode: str) -> str:
    """Pick what a Bash prompt shows, per the Flipper's `perm_detail` setting.

    The preference lives on the device (Menu -> "Prompt: …") and arrives via
    `hello` or a `pref` message, so the choice belongs to whoever is holding
    the Flipper and about to approve something.
    """
    description = (description or "").strip()
    command = strip_command_noise(command or "").strip()
    if mode == "command":
        return fit_detail(command or description)
    if mode == "both" and description and command:
        # Description first so it survives truncation; command fills the rest.
        return fit_detail(f"{description}: {command}")
    return fit_detail(description or command)


def perm_msg(tool: str, detail: str = "") -> bytes:
    d: dict = {"tool": tool[:PERM_TOOL_MAX]}
    if detail:
        d["detail"] = detail[:PERM_DETAIL_MAX]
    return encode("perm", d)
