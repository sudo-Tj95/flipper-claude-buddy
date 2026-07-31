"""JSON protocol for Flipper <-> Host Bridge communication."""

import json
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


def perm_msg(tool: str, detail: str = "") -> bytes:
    d: dict = {"tool": tool[:PERM_TOOL_MAX]}
    if detail:
        d["detail"] = detail[:PERM_DETAIL_MAX]
    return encode("perm", d)
