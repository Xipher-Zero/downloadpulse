"""The desktop application's external address: Local Port.

The installed desktop application's window talks to its backend on a private
loopback port the supervisor chooses at each launch: the one it had last time
while that is still free, so the window's address and its browser cache stay
the same, else a free one (``desktop/supervisor.py`` ``window_port``). External clients -- the download-client dialects, the
native external API, a browser extension -- need an address that stays put:
this listener, on ``127.0.0.1`` at the installation's Local Port
(``AppSettings.desktop_local_port``, 8095 unless changed).

It is a byte-for-byte relay to the backend's own internal port, so every
request it carries meets exactly the same application: the same middleware,
Host guard, body limits, authentication and streaming. It is not a second
HTTP server (``uvicorn.Server.serve`` would take over the process's signal
handlers), and it listens on loopback only.

A change of Local Port applies at once (``move``): the new port is bound
first, and only once it listens is the port saved and the old one closed, so
a port that is taken changes nothing. Connections already open on the old
port finish on their own. Port taken at launch: the application runs
regardless, and the listener stays down -- the supervisor tells the person
(``state()['listening']`` is false) -- until a save binds one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress

logger = logging.getLogger("downloadpulse.desktop_listener")

HOST = "127.0.0.1"
LOWEST, HIGHEST = 1024, 65535
CHUNK = 64 * 1024
# How long a closed relay connection's address still reads as Local Port's.
# The server starts no further pipelined request on a connection once it is
# closing (uvicorn ``on_response_complete``), and it sees this relay's close in
# moments; until then the address must not read as the window's. Meanwhile the
# closing side's TIME_WAIT keeps it from being another connection's.
RELAYED_AFTER_CLOSE_SECONDS = 60.0
INTERNAL_PORT_ENV = "DOWNLOADPULSE_DESKTOP_PORT"


class PortInUse(OSError):
    """The port could not be bound: another program holds it."""


class PortInvalid(ValueError):
    """Not a port a Local Port may be (a whole number 1024-65535)."""


def valid_port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not LOWEST <= value <= HIGHEST:
        raise PortInvalid(value)
    return value


def bind(port: int) -> socket.socket:
    """A listening loopback socket on ``port``, or ``PortInUse``. On Windows
    exclusively (``SO_EXCLUSIVEADDRUSE``: nothing may share it); elsewhere with
    ``SO_REUSEADDR``, which lets a port this application just released be
    taken again at once but never one another program is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        raise PortInUse(str(exc)) from None
    return sock


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


class LocalListener:
    """The one Local Port listener of this backend process."""

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self._port: int | None = None             # the port listened on, while listening
        self._target: int | None = None           # the backend's own internal port
        self._lock = asyncio.Lock()
        self._connections: set[asyncio.StreamWriter] = set()
        # The local address of each connection this relay opened to the
        # backend -- the client address the backend sees for every request it
        # carries (``relays``) -- counted, as one address may be reused.
        self._relayed: dict[tuple[str, int], int] = {}
        self._forgetting: set[asyncio.TimerHandle] = set()   # each pending ``_forget``, until it runs or ``stop``

    def state(self, configured: int) -> dict:
        """``port``: the configured Local Port; ``listening``: whether this
        process listens on it now."""
        return {"port": configured, "listening": self._server is not None and self._port == configured}

    def relays(self, client) -> bool:
        """Whether ``client`` -- the address a request reached the backend
        from -- is one of this relay's own connections to it: the request came
        through Local Port. The kernel chose that address when the relay
        connected; nothing a client sends can name it."""
        return client is not None and (str(client[0]), int(client[1])) in self._relayed

    def _forget(self, relayed: tuple[str, int], handle: list) -> None:
        self._forgetting.discard(handle[0])
        if self._relayed.get(relayed, 0) > 1:
            self._relayed[relayed] -= 1
        else:
            self._relayed.pop(relayed, None)

    async def _relay(self, client_reader, client_writer) -> None:
        self._connections.add(client_writer)
        backend_writer = relayed = None
        try:
            backend_reader, backend_writer = await asyncio.open_connection(HOST, self._target)
            self._connections.add(backend_writer)
            # Recorded before a byte is relayed, so no request it carries is
            # ever seen without it.
            relayed = tuple(backend_writer.get_extra_info("sockname")[:2])
            self._relayed[relayed] = self._relayed.get(relayed, 0) + 1
            await asyncio.gather(_pipe(client_reader, backend_writer), _pipe(backend_reader, client_writer))
        except (ConnectionError, OSError):
            pass
        finally:
            for writer in (client_writer, backend_writer):
                if writer is not None:
                    self._connections.discard(writer)
                    with suppress(OSError, RuntimeError):
                        writer.close()
            if relayed is not None:
                # The backend may still be serving a request this connection
                # carried (a client that pipelined one, then went): the address
                # stays relayed until the connection is closed and a while
                # after (``RELAYED_AFTER_CLOSE_SECONDS``), never forgotten first.
                with suppress(OSError, RuntimeError, ConnectionError):
                    await backend_writer.wait_closed()
                handle: list = []
                handle.append(asyncio.get_running_loop().call_later(
                    RELAYED_AFTER_CLOSE_SECONDS, self._forget, relayed, handle))
                self._forgetting.add(handle[0])

    async def _serve(self, sock: socket.socket) -> asyncio.base_events.Server:
        return await asyncio.start_server(self._relay, sock=sock)

    async def start(self, port: int, target: int) -> bool:
        """Listen at launch; a taken port leaves the listener down (and says
        so in the log), never the application."""
        async with self._lock:
            self._target = target
            try:
                sock = bind(port)
            except PortInUse as exc:
                logger.warning("Local Port %s is in use (%s): external clients cannot reach DownloadPulse "
                               "until it is free or changed", port, exc)
                return False
            self._server, self._port = await self._serve(sock), port
            logger.info("external clients reach DownloadPulse at http://%s:%s", HOST, port)
            return True

    async def move(self, port: int, persist) -> None:
        """Listen on ``port`` instead: bind it, start serving it, ``persist()``
        it (the saved setting), then close the old one. ``PortInUse`` or a
        failed ``persist`` leaves the old port listening and nothing saved.
        The current port again re-binds it only if it is down (a port freed
        since launch)."""
        async with self._lock:
            if self._server is not None and port == self._port:
                await persist()
                return
            sock = bind(port)
            server = await self._serve(sock)
            try:
                await persist()
            except BaseException:
                server.close()
                raise
            old, self._server, self._port = self._server, server, port
            if old is not None:
                old.close()                  # stops accepting; open connections finish on their own
            logger.info("external clients now reach DownloadPulse at http://%s:%s", HOST, port)

    async def stop(self) -> None:
        async with self._lock:
            server, self._server, self._port = self._server, None, None
            for pending in self._forgetting:
                pending.cancel()
            self._forgetting.clear()
            self._relayed.clear()            # no address it carried reads as relayed once it has stopped
            if server is None:
                return
            server.close()
            for writer in list(self._connections):
                with suppress(OSError, RuntimeError):
                    writer.close()
            with suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)


listener = LocalListener()


def configured_port() -> int:
    """The installation's Local Port (``AppSettings.desktop_local_port``); the
    default when the saved value is not a port a Local Port may be."""
    from core.config import AppSettings, get_settings
    try:
        return valid_port(get_settings().desktop_local_port)
    except PortInvalid:
        return AppSettings.model_fields["desktop_local_port"].default


def internal_port() -> int | None:
    """The backend's own port, as its supervisor started it."""
    try:
        return valid_port(int(os.environ.get(INTERNAL_PORT_ENV, "")))
    except (ValueError, PortInvalid):
        return None


def window_request(request) -> bool:
    """Whether a request came to the window's own address -- the private port
    the supervisor loads the window from (``http://127.0.0.1:<port>/``) -- and
    not through Local Port. Decided by the connection it arrived on, never by
    anything its client sent (a ``Host`` header is the client's to write):
    ``request.client`` is the request's peer address (ASGI ``scope["client"]``,
    which the supervisor's uvicorn never rewrites from a forwarding header:
    ``--no-proxy-headers``), and a request Local Port carried arrives from one
    of the relay's own connections (``LocalListener.relays``). The window
    sends no forwarding header, so a request carrying one (``X-Forwarded-For``,
    ``Forwarded``) is never the window's. (A browser pointed at the private
    port by hand would come to it directly too. The port is no secret -- it is
    remembered between launches and on the backend's command line -- and the
    guard never relied on it: it is the connection the request arrived on,
    direct and not through Local Port's relay, from this machine's loopback.)"""
    client = request.client
    return (internal_port() is not None and client is not None and not listener.relays(client)
            and "x-forwarded-for" not in request.headers and "forwarded" not in request.headers)
