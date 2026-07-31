#!/usr/bin/env python3
"""Concurrent permission requests must queue on the Flipper, not be dropped.

Claude Code issues tool calls in parallel. Upstream's bridge holds a single
pending-permission slot and answers `busy` to anything that arrives while a
prompt is on screen; the hook then exits non-zero and Claude falls back to the
desktop permission dialog. That is precisely the wrong failure mode for a
device whose entire purpose is approving things while you are away from the
machine.

Run directly — the repo has no test framework and the bridge has no test-only
dependencies:

    python3 plugin/host-bridge/tests/test_permission_queue.py
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bridge import daemon as daemon_mod  # noqa: E402
from bridge.daemon import Daemon  # noqa: E402
from bridge.transport import Transport  # noqa: E402


class FakeTransport(Transport):
    """Records everything written to the device; never delivers anything."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def connect(self) -> bool:
        return True

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def is_closing(self) -> bool:
        return False


def make_daemon() -> tuple[Daemon, FakeTransport]:
    transport = FakeTransport()
    # Dictation/input backends are untouched by the permission path; passing
    # sentinels avoids constructing the real AppleScript backends.
    d = Daemon(transport, dictation=object(), input_backend=object())
    d.serial.connected = True
    return d, transport


def prompts_sent(transport: FakeTransport) -> list[bytes]:
    return [w for w in transport.writes if b'"perm"' in w]


async def settle(times: int = 6) -> None:
    """Let queued coroutines reach their next await point."""
    for _ in range(times):
        await asyncio.sleep(0)


def request(detail: str) -> dict:
    return {"action": "permission_request", "tool": "Bash", "detail": detail}


async def answer(d: Daemon, allow: bool) -> None:
    await d._handle_flipper_msg({"t": "perm_resp", "d": {"allow": allow}})
    await settle()


# ── Tests ──────────────────────────────────────────────────────────


async def test_second_request_is_queued_not_rejected():
    """A prompt arriving while another is on screen must wait, not get `busy`.

    Fails against the single-slot implementation, which returns
    {"status": "busy"} immediately and sends the decision back to the laptop.
    """
    d, transport = make_daemon()

    first = asyncio.create_task(d._handle_ipc_action(request("first")))
    await settle()
    second = asyncio.create_task(d._handle_ipc_action(request("second")))
    await settle()

    assert not second.done(), (
        "second request returned immediately (result=%r) — it was rejected "
        "rather than queued" % (second.result() if second.done() else None)
    )
    assert len(prompts_sent(transport)) == 1, (
        "expected only the first prompt on screen, got %d"
        % len(prompts_sent(transport))
    )

    await answer(d, True)
    assert (await first)["allowed"] is True
    await answer(d, False)
    assert (await second)["allowed"] is False


async def test_queued_request_is_displayed_after_first_resolves():
    """The queued prompt must actually reach the device, in order."""
    d, transport = make_daemon()

    first = asyncio.create_task(d._handle_ipc_action(request("alpha")))
    await settle()
    second = asyncio.create_task(d._handle_ipc_action(request("bravo")))
    await settle()

    sent = prompts_sent(transport)
    assert len(sent) == 1 and b"alpha" in sent[0], "first prompt not displayed"
    assert not any(b"bravo" in w for w in transport.writes), (
        "queued prompt was displayed while another was still on screen"
    )

    await answer(d, True)

    sent = prompts_sent(transport)
    assert len(sent) == 2, "queued prompt never displayed (got %d)" % len(sent)
    assert b"bravo" in sent[1], "queued prompt displayed out of order"

    await answer(d, True)
    await first
    await second


async def test_each_queued_request_gets_its_own_answer():
    """Answers must map to the prompt on screen, not leak across requests."""
    d, _ = make_daemon()

    first = asyncio.create_task(d._handle_ipc_action(request("one")))
    await settle()
    second = asyncio.create_task(d._handle_ipc_action(request("two")))
    await settle()

    await answer(d, False)  # deny the first
    await answer(d, True)  # allow the second

    assert (await first)["allowed"] is False, "first got the second's answer"
    assert (await second)["allowed"] is True, "second got the first's answer"


async def test_queue_depth_is_capped():
    """Beyond the cap, fall back to the desktop rather than build a backlog.

    A burst of parallel tool calls should not leave a queue of prompts to
    click through minutes later — by then the context is gone.
    """
    d, _ = make_daemon()
    cap = daemon_mod.PERM_MAX_QUEUED

    tasks = []
    for i in range(cap + 1):  # one on screen + `cap` queued
        tasks.append(asyncio.create_task(d._handle_ipc_action(request("q%d" % i))))
        await settle()

    overflow = asyncio.create_task(d._handle_ipc_action(request("overflow")))
    await settle()

    assert overflow.done(), "request past the cap should be rejected immediately"
    assert (await overflow)["status"] == "busy", (
        "expected busy past the cap, got %r" % (await overflow)
    )

    for _ in tasks:
        await answer(d, True)
    for t in tasks:
        await t


async def test_abandoned_prompt_promotes_the_queued_one():
    """A prompt whose hook died must not hold the queue.

    Observed live: a request was answered in the desktop dialog, its hook
    exited, and the bridge kept that prompt on the device for its full budget.
    Everything behind it waited, and the ALLOW pressed on the Flipper resolved
    the dead request — so the button did nothing anywhere. ClaudeIPC cancels
    the action when the hook disconnects; this asserts the daemon reacts by
    handing the screen to the next in line.
    """
    d, transport = make_daemon()

    stale = asyncio.create_task(d._handle_ipc_action(request("stale")))
    await settle()
    queued = asyncio.create_task(d._handle_ipc_action(request("next")))
    await settle()
    assert len(prompts_sent(transport)) == 1, "queued prompt displayed too early"

    stale.cancel()  # what ClaudeIPC does when the hook goes away
    await settle()

    sent = prompts_sent(transport)
    assert len(sent) == 2, "queued prompt never promoted (got %d)" % len(sent)
    assert b"next" in sent[1], "wrong prompt promoted"

    await answer(d, True)
    assert (await queued)["allowed"] is True


async def test_hook_socket_timeout_outlasts_the_bridge_queue():
    """The hook must not hang up on a prompt the bridge is still showing.

    Queueing means a request can wait PERM_QUEUE_WAIT_TIMEOUT before it is even
    displayed, then PERM_DISPLAY_TIMEOUT on screen. If the hook's socket
    timeout is shorter it abandons the request mid-prompt: the user presses
    ALLOW on a prompt whose answer no longer has anywhere to go, and Claude has
    already fallen back to the desktop dialog.
    """
    import importlib.util

    hook_path = (
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "on-permission-request.py"
    )
    spec = importlib.util.spec_from_file_location("_perm_hook", hook_path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    worst_case = daemon_mod.PERM_QUEUE_WAIT_TIMEOUT + daemon_mod.PERM_DISPLAY_TIMEOUT
    assert hook.TIMEOUT > worst_case, (
        "hook TIMEOUT=%gs does not cover the bridge's worst case of %gs "
        "(queue %gs + display %gs)"
        % (
            hook.TIMEOUT,
            worst_case,
            daemon_mod.PERM_QUEUE_WAIT_TIMEOUT,
            daemon_mod.PERM_DISPLAY_TIMEOUT,
        )
    )


# ── Runner ─────────────────────────────────────────────────────────


async def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            await asyncio.wait_for(t(), timeout=10)
        except Exception as e:
            failures += 1
            print("FAIL  %s\n        %s: %s" % (t.__name__, type(e).__name__, e))
        else:
            print("ok    %s" % t.__name__)
    print("\n%d passed, %d failed" % (len(tests) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
