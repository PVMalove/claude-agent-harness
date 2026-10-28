"""Project-bound asyncio console transport; optional dependencies load only at startup."""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets

from .discovery import Discovery
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import h11
    from websockets.server import ServerProtocol

HEADER_LIMIT = 16384
HEADER_TIMEOUT = 5.0
CLOSE_TIMEOUT = 0.5
READ_CHUNK = 16384
MESSAGE_LIMIT = 65536

# This independent logger never propagates protocol headers, even under root DEBUG.
_PROTOCOL_LOGGER = logging.Logger("harness.console.protocol")
_PROTOCOL_LOGGER.disabled = True
_PROTOCOL_LOGGER.propagate = False

_SECURITY_HEADERS = [
    (b"Cache-Control", b"no-store"),
    (b"X-Content-Type-Options", b"nosniff"),
    (b"X-Frame-Options", b"DENY"),
    (
        b"Content-Security-Policy",
        b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    ),
]


class ConsoleServer:
    """One loopback listener for one project; callers own its asynchronous lifetime."""

    def __init__(
        self,
        repo: Path,
        *,
        idle_grace_seconds: float = 30,
        active_jobs: Callable[[], int] = lambda: 0,
    ) -> None:
        self.repo = repo.resolve()
        self._idle_grace = idle_grace_seconds
        self._active_jobs = active_jobs
        self._clients = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._listener: asyncio.Server | None = None
        self._discovery: Discovery | None = None
        self._token = ""
        self._port = 0
        self._started = False
        self._closing = False
        self._closed = asyncio.Event()
        self._connections: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        """The OS-assigned IPv4 port, available after start()."""
        if not self._port:
            raise RuntimeError("console server has not started")
        return self._port

    async def start(self) -> None:
        if self._started or self._closing:
            raise RuntimeError("console server cannot be restarted")
        importlib.import_module("h11")
        importlib.import_module("websockets.server")
        self._started = True
        try:
            self._discovery = Discovery(self.repo)
            self._discovery.acquire()
            self._listener = await asyncio.start_server(
                self._accept, "127.0.0.1", 0, start_serving=False
            )
            self._port = self._listener.sockets[0].getsockname()[1]
            self._token = secrets.token_hex(32)
            self._discovery.publish(self._port, self._token)
            await self._listener.start_serving()
            self._idle_task = asyncio.create_task(self._exit_when_idle())
        except BaseException:
            await self.close()
            raise

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        import h11

        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=HEADER_LIMIT)
        request: h11.Request | None = None
        handed_off = False
        try:
            prefix = bytearray()
            async with asyncio.timeout(HEADER_TIMEOUT):
                while request is None:
                    event = connection.next_event()
                    if isinstance(event, h11.Request):
                        request = event
                    elif event is h11.NEED_DATA:
                        data = await reader.read(READ_CHUNK)
                        if not data:
                            return
                        prefix.extend(data)
                        connection.receive_data(data)
                    else:
                        return
            # h11 supplies the consumed-byte boundary, including when a body/frame
            # arrives in the same read. Never scan or reconstruct the raw envelope.
            tail, eof = connection.trailing_data
            boundary = len(prefix) - len(tail)
            if boundary > HEADER_LIMIT:
                await self._http(connection, request, reader, writer, 431)
                return
            status = self._admission(request)
            if status is not None:
                await self._http(connection, request, reader, writer, status)
                return
            if request.target != b"/ws":
                await self._http(
                    connection,
                    request,
                    reader,
                    writer,
                    200 if request.target == b"/" else 404,
                )
                return
            if request.method != b"GET":
                await self._http(connection, request, reader, writer, 405)
                return
            if not self._authenticated(request):
                await self._http(connection, request, reader, writer, 401)
                return
            if any(
                name == b"transfer-encoding"
                or (name == b"content-length" and int(value) != 0)
                for name, value in request.headers
            ):
                await self._http(connection, request, reader, writer, 400)
                return
            end = connection.next_event()
            paused = connection.next_event()
            if not isinstance(end, h11.EndOfMessage) or paused is not h11.PAUSED or eof:
                await self._http(connection, request, reader, writer, 400)
                return
            envelope = bytes(prefix[:boundary])
            del prefix
            handed_off = True
            await self._websocket(envelope, tail, reader, writer)
        except h11.RemoteProtocolError as exc:
            await self._http(connection, request, reader, writer, exc.error_status_hint)
        except TimeoutError:
            if not handed_off:
                await self._http(connection, request, reader, writer, 408)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await writer.wait_closed()
            except (TimeoutError, ConnectionError, OSError):
                pass

    def _admission(self, request: h11.Request) -> int | None:
        host = [value for name, value in request.headers if name == b"host"]
        origin = [value for name, value in request.headers if name == b"origin"]
        expected = f"127.0.0.1:{self.port}".encode()
        if host != [expected] or origin not in ([], [b"http://" + expected]):
            return 403
        if request.method not in (b"GET", b"HEAD"):
            return 405
        return None

    def _authenticated(self, request: h11.Request) -> bool:
        auth = [value for name, value in request.headers if name == b"authorization"]
        cookies = [value for name, value in request.headers if name == b"cookie"]
        origin = [value for name, value in request.headers if name == b"origin"]
        if len(auth) == 1 and not cookies:
            return secrets.compare_digest(auth[0], b"Bearer " + self._token.encode())
        if (
            len(cookies) == 1
            and not auth
            and origin == [f"http://127.0.0.1:{self.port}".encode()]
        ):
            return secrets.compare_digest(
                cookies[0], b"console_token=" + self._token.encode()
            )
        return False

    async def _http(
        self,
        connection: h11.Connection,
        request: h11.Request | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        status: int,
    ) -> None:
        import h11

        body = b"Console server\n" if status == 200 else b"Request rejected\n"
        headers = [
            *_SECURITY_HEADERS,
            (b"Content-Type", b"text/plain; charset=utf-8"),
            (b"Content-Length", str(len(body)).encode()),
            (b"Connection", b"close"),
        ]
        if status == 405:
            headers.append((b"Allow", b"GET, HEAD"))
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                writer.write(
                    connection.send(h11.Response(status_code=status, headers=headers))
                )
                if request is None or request.method != b"HEAD":
                    writer.write(connection.send(h11.Data(data=body)))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
                # Flush the final response before reading a withheld or large body.
                # h11 drains framing; the application never buffers request bodies.
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        data = await reader.read(READ_CHUNK)
                        connection.receive_data(data)
                        if not data:
                            break
                    elif event is h11.PAUSED or isinstance(
                        event, (h11.EndOfMessage, h11.ConnectionClosed)
                    ):
                        break
        except (
            TimeoutError,
            ConnectionError,
            OSError,
            h11.RemoteProtocolError,
            h11.LocalProtocolError,
        ):
            pass

    async def _flush_websocket(
        self, protocol: ServerProtocol, writer: asyncio.StreamWriter
    ) -> bool:
        ended = False
        for data in protocol.data_to_send():
            if data:
                writer.write(data)
            else:
                ended = True
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await writer.drain()
        if ended and writer.can_write_eof():
            writer.write_eof()
        return ended

    async def _websocket(
        self,
        envelope: bytes,
        tail: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        from websockets.http11 import Request
        from websockets.server import ServerProtocol
        from websockets.typing import Origin

        protocol = ServerProtocol(
            origins=[Origin(f"http://127.0.0.1:{self.port}"), None],
            extensions=None,
            subprotocols=None,
            max_size=MESSAGE_LIMIT,
            logger=_PROTOCOL_LOGGER,
        )
        protocol.receive_data(envelope)
        events = protocol.events_received()
        if len(events) != 1 or not isinstance(events[0], Request):
            await self._flush_websocket(protocol, writer)
            return
        response = protocol.accept(events[0])
        if self._closing:
            response = protocol.reject(503, "Console is closing\n")
        protocol.send_response(response)
        if await self._flush_websocket(protocol, writer) or response.status_code != 101:
            return
        if tail:
            protocol.receive_data(tail)
        self._clients += 1
        try:
            await self._pump_websocket(protocol, reader, writer)
        finally:
            self._clients -= 1

    async def _pump_websocket(
        self,
        protocol: ServerProtocol,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        from websockets.frames import Frame, Opcode

        closing_at: float | None = None
        while True:
            for event in protocol.events_received():
                if isinstance(event, Frame) and event.opcode in (
                    Opcode.TEXT,
                    Opcode.BINARY,
                    Opcode.CONT,
                ):
                    if not protocol.close_expected():
                        protocol.send_close(1003, "No application protocol")
            if await self._flush_websocket(protocol, writer):
                return
            if protocol.close_expected():
                if closing_at is None:
                    closing_at = asyncio.get_running_loop().time() + CLOSE_TIMEOUT
                async with asyncio.timeout_at(closing_at):
                    data = await reader.read(READ_CHUNK)
            else:
                data = await reader.read(READ_CHUNK)
            if not data:
                protocol.receive_eof()
                await self._flush_websocket(protocol, writer)
                return
            protocol.receive_data(data)

    async def _exit_when_idle(self) -> None:
        """Close once no client and no job has been present for the grace period."""
        loop = asyncio.get_running_loop()
        poll = min(1.0, self._idle_grace / 4)
        idle_since = loop.time()
        while True:
            await asyncio.sleep(poll)
            if self._clients or self._active_jobs():
                idle_since = loop.time()
            elif loop.time() - idle_since >= self._idle_grace:
                asyncio.create_task(self.close())
                return

    async def close(self) -> None:
        """Idempotently close the listener and all of this instance's connections."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        try:
            if self._idle_task is not None:
                self._idle_task.cancel()
            if self._listener is not None:
                self._listener.close()
                await self._listener.wait_closed()
            tasks = list(self._connections)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                if self._discovery is not None:
                    self._discovery.close()
            finally:
                self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()


async def _serve(repo: Path) -> None:
    server = ConsoleServer(repo)
    try:
        await server.start()
        await server.wait_closed()
    finally:
        await server.close()


def run(repo: Path) -> None:
    """Serve the project until the idle rule stops the server; Ctrl-C ends it quietly."""
    try:
        asyncio.run(_serve(repo))
    except KeyboardInterrupt:
        pass
