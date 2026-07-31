"""Unix domain socket server for Claude Code hooks IPC.

Simple request/response: hooks connect, send a JSON action, get a response.
"""

import asyncio
import json
import logging
import os

from . import config

log = logging.getLogger(__name__)

# How often to prove the requesting hook is still there while its action is
# pending. Permission requests can legitimately wait minutes for someone to
# reach the Flipper, and during that time the bridge holds the device's single
# permission view — so a hook that has been abandoned by Claude Code must be
# noticed, or it blocks every queued prompt behind it.
#
# Detecting this is fiddlier than it looks. The hook half-closes its write side
# as soon as it has sent the request (`shutdown(SHUT_WR)`), so read-EOF arrives
# on EVERY request and says nothing about liveness; on macOS POLLHUP is set in
# both cases too. Attempting a write is the only thing that distinguishes them:
# a dead peer raises BrokenPipeError.
HOOK_KEEPALIVE_INTERVAL = 5.0

# A bare newline. Responses are newline-delimited JSON, so a keepalive is an
# empty record — invisible to any reader that skips blank lines, which is what
# decode_hook_response() does.
HOOK_KEEPALIVE = b"\n"


def decode_hook_response(raw: bytes) -> dict:
    """Parse a bridge response, ignoring keepalives that may precede it.

    Shared with the hooks, which read a single recv() and would otherwise
    choke on a keepalive that arrived ahead of the payload.
    """
    for line in raw.split(b"\n"):
        line = line.strip()
        if line:
            return json.loads(line.decode())
    raise ValueError("no response payload in %r" % raw)


class ClaudeIPC:
    def __init__(self):
        self._server: asyncio.Server | None = None
        self._on_action = None

    def on_action(self, callback):
        self._on_action = callback

    async def start(self):
        if os.path.exists(config.SOCKET_PATH):
            os.unlink(config.SOCKET_PATH)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=config.SOCKET_PATH
        )
        os.chmod(config.SOCKET_PATH, 0o666)
        log.info("IPC listening on %s", config.SOCKET_PATH)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=10.0)
            if not data:
                return

            request = json.loads(data.decode().strip())

            if self._on_action:
                # Run the action alongside a watchdog that abandons it if the
                # hook goes away. Without this an abandoned permission request
                # keeps the device's only permission view for its full budget,
                # blocking every queued prompt behind it — and the button press
                # that eventually arrives resolves the dead request, so the
                # user presses ALLOW and nothing happens anywhere.
                action = asyncio.ensure_future(self._on_action(request))
                watchdog = asyncio.ensure_future(self._abandon_if_peer_gone(writer, action))
                try:
                    response = await action
                except asyncio.CancelledError:
                    log.info("IPC: hook disconnected — request abandoned")
                    return
                finally:
                    watchdog.cancel()
            else:
                response = {"status": "ok"}

            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except asyncio.TimeoutError:
            log.warning("IPC: read timeout")
        except (ConnectionError, BrokenPipeError):
            log.debug("IPC: client disconnected")
        except Exception as e:
            log.error("IPC: error: %s", e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _abandon_if_peer_gone(self, writer: asyncio.StreamWriter, action: asyncio.Future):
        """Cancel *action* once the hook that asked for it has exited.

        Writing is the only reliable probe — see HOOK_KEEPALIVE_INTERVAL. The
        keepalive is a blank line, which decode_hook_response() skips, so a
        live hook is unaffected by being probed.
        """
        try:
            while not action.done():
                await asyncio.sleep(HOOK_KEEPALIVE_INTERVAL)
                if action.done():
                    return
                try:
                    writer.write(HOOK_KEEPALIVE)
                    await writer.drain()
                except (ConnectionError, BrokenPipeError, OSError) as e:
                    log.info("IPC: hook went away (%s) — abandoning its request", type(e).__name__)
                    action.cancel()
                    return
        except asyncio.CancelledError:
            pass

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(config.SOCKET_PATH):
            os.unlink(config.SOCKET_PATH)
