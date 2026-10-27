"""Project-bound asyncio console transport; optional dependencies load only at startup."""

from __future__ import annotations

import argparse
import asyncio
import importlib
from pathlib import Path

H11_PIN = "0.16.0"
WEBSOCKETS_PIN = "17.3"
CONSOLE_SERVER_REQUIREMENTS = (f"h11=={H11_PIN}", f"websockets=={WEBSOCKETS_PIN}")


class ConsoleServer:
    """One loopback listener for one project; callers own its asynchronous lifetime."""

    def __init__(self, repo: Path, *, idle_grace_seconds: float = 30) -> None:
        self.repo = repo.resolve()
        self._listener: asyncio.Server | None = None
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
            self._listener = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            self._port = self._listener.sockets[0].getsockname()[1]
        except BaseException:
            await self.close()
            raise

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import h11

        try:
            connection = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
            async with asyncio.timeout(5):
                while True:
                    event = connection.next_event()
                    if isinstance(event, h11.Request):
                        body = b"Console server\n"
                        writer.write(connection.send(h11.Response(status_code=200, headers=[
                            (b"Content-Length", str(len(body)).encode()), (b"Connection", b"close")
                        ])))
                        if event.method != b"HEAD":
                            writer.write(connection.send(h11.Data(data=body)))
                        writer.write(connection.send(h11.EndOfMessage()))
                        await writer.drain()
                        return
                    if event is not h11.NEED_DATA:
                        return
                    data = await reader.read(16384)
                    if not data:
                        return
                    connection.receive_data(data)
        except (TimeoutError, ConnectionError, h11.RemoteProtocolError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def close(self) -> None:
        """Idempotently close the listener and all of this instance's connections."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        try:
            if self._listener is not None:
                self._listener.close()
                await self._listener.wait_closed()
            tasks = list(self._connections)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the project-bound console server")
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(_serve(args.project))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
