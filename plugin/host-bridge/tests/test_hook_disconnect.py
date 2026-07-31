#!/usr/bin/env python3
"""A prompt whose hook has gone away must not hold the device hostage.

Queueing permission prompts introduced head-of-line blocking. If Claude Code
abandons a request — the user answered in the desktop dialog, or the turn was
interrupted — the hook process dies, but the bridge kept showing that prompt
for its full 60s budget and everything behind it waited. Worse, the button
press that finally arrived was applied to the dead prompt, so the user pressed
ALLOW and nothing happened anywhere.

The hook half-closes its write side immediately (`shutdown(SHUT_WR)`), so
read-EOF arrives on every request and cannot be used as a liveness signal. On
macOS POLLHUP is likewise set in both cases. The only thing that distinguishes
a live hook from a dead one is attempting a write, so the bridge sends
keepalives while an action is pending.

    python3 plugin/host-bridge/tests/test_hook_disconnect.py
"""

import asyncio
import pathlib
import socket
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bridge import claude_ipc as ipc_mod  # noqa: E402
from bridge import config  # noqa: E402
from bridge.claude_ipc import ClaudeIPC  # noqa: E402


class Harness:
    """A real ClaudeIPC on a throwaway socket, with a controllable action."""

    def __init__(self) -> None:
        self.ipc = ClaudeIPC()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.completed = asyncio.Event()
        self.release = asyncio.Event()
        self.path = str(pathlib.Path(tempfile.mkdtemp()) / "bridge.sock")
        self._orig_path = config.SOCKET_PATH
        self._orig_interval = ipc_mod.HOOK_KEEPALIVE_INTERVAL

    async def __aenter__(self) -> "Harness":
        config.SOCKET_PATH = self.path
        ipc_mod.HOOK_KEEPALIVE_INTERVAL = 0.05
        self.ipc.on_action(self._action)
        await self.ipc.start()
        return self

    async def _action(self, request: dict) -> dict:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed.set()
        return {"status": "ok", "allowed": True}

    def connect(self) -> socket.socket:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(self.path)
        c.sendall(b'{"action":"permission_request","tool":"Bash"}')
        c.shutdown(socket.SHUT_WR)  # exactly what the real hook does
        return c

    async def __aexit__(self, *exc) -> None:
        self.release.set()
        await self.ipc.stop()
        config.SOCKET_PATH = self._orig_path
        ipc_mod.HOOK_KEEPALIVE_INTERVAL = self._orig_interval


# ── Tests ──────────────────────────────────────────────────────────


async def test_pending_action_is_cancelled_when_hook_disconnects():
    """Killing the client must cancel the in-flight action.

    Without keepalives the bridge never notices and the action runs to its
    full timeout, blocking every queued prompt behind it.
    """
    async with Harness() as h:
        client = h.connect()
        await asyncio.wait_for(h.started.wait(), timeout=2)

        client.close()  # hook process dies

        try:
            await asyncio.wait_for(h.cancelled.wait(), timeout=3)
        except asyncio.TimeoutError:
            raise AssertionError(
                "action was not cancelled after the hook disconnected — "
                "the prompt would stay on the device for its full budget"
            )


async def test_live_hook_is_not_cancelled_by_keepalives():
    """Keepalives must not mistake a patiently waiting hook for a dead one.

    This is the regression that matters: a hook waiting the full 185s for
    someone to walk to the Flipper looks identical to a dead one at the socket
    level apart from the write result.
    """
    async with Harness() as h:
        client = h.connect()
        await asyncio.wait_for(h.started.wait(), timeout=2)

        # Sit through many keepalive intervals without touching the client.
        await asyncio.sleep(ipc_mod.HOOK_KEEPALIVE_INTERVAL * 10)

        assert not h.cancelled.is_set(), "live hook was wrongly cancelled"

        h.release.set()
        await asyncio.wait_for(h.completed.wait(), timeout=2)
        client.close()


async def test_response_still_parses_after_keepalives():
    """Keepalive bytes must not corrupt the response the hook reads.

    The hook does a single recv() then json.loads(). If keepalives land in that
    buffer the decode fails and the hook falls back to the desktop dialog —
    trading a blocked queue for a broken one.
    """
    async with Harness() as h:
        client = h.connect()
        await asyncio.wait_for(h.started.wait(), timeout=2)
        await asyncio.sleep(ipc_mod.HOOK_KEEPALIVE_INTERVAL * 4)

        h.release.set()
        await asyncio.wait_for(h.completed.wait(), timeout=2)

        loop = asyncio.get_running_loop()
        client.setblocking(False)
        raw = b""
        for _ in range(50):
            try:
                chunk = await loop.sock_recv(client, 4096)
            except (BlockingIOError, ConnectionError):
                break
            if not chunk:
                break
            raw += chunk
            if b"}" in raw:
                break
        client.close()

        decoded = ipc_mod.decode_hook_response(raw)
        assert decoded.get("status") == "ok", (
            "hook could not parse its response out of %r" % raw
        )


def load_hook():
    import importlib.util

    path = (
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "on-permission-request.py"
    )
    spec = importlib.util.spec_from_file_location("_perm_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_hook_reads_its_payload_past_keepalives():
    """The real hook must survive keepalives arriving before its answer.

    It does one recv() then json.loads(). A keepalive landing first makes that
    decode fail, the hook exits non-zero, and Claude falls back to the desktop
    dialog — turning the head-of-line fix into a different way to lose the
    prompt.
    """
    hook = load_hook()
    path = str(pathlib.Path(tempfile.mkdtemp()) / "bridge.sock")
    hook.SOCKET_PATH = path
    payload = {"status": "ok", "allowed": True, "always": False}

    async def serve(reader, writer):
        await reader.read(65536)
        for _ in range(3):  # prompt sitting on the device
            writer.write(ipc_mod.HOOK_KEEPALIVE)
            await writer.drain()
            await asyncio.sleep(0.02)
        writer.write(__import__("json").dumps(payload).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(serve, path=path)
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, hook.send_to_bridge, "Bash", "detail", ""),
            timeout=10,
        )
        assert result == payload, "hook returned %r" % (result,)
    finally:
        server.close()
        await server.wait_closed()


# ── Runner ─────────────────────────────────────────────────────────


async def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            await asyncio.wait_for(t(), timeout=20)
        except Exception as e:
            failures += 1
            print("FAIL  %s\n        %s: %s" % (t.__name__, type(e).__name__, e))
        else:
            print("ok    %s" % t.__name__)
    print("\n%d passed, %d failed" % (len(tests) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
