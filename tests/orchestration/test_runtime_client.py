"""The Python client of ``harness-runtime`` and the ``runtime status`` command (ADR 0021).

Here a scripted runtime stands at the Unix socket (on Windows, at a named pipe), so each rule of
the client is observed at the connection: the address, the token, the retry of one ``operation_id``
and the typed errors with exit code 2. The contract tests run unchanged on both transports; only
the tests of Unix file modes and of the Unix socket path limit are Unix-only. The seam with the
real Rust process, driven through the coordinator CLI, is at the end of the module; CI builds the
binary first (``HARNESS_REQUIRE_RUNTIME=1`` makes a missing one fail).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, cast

import pytest

from harness import errors
from harness.orchestration import coordinator, runtime_client
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

if sys.platform == "win32":
    import _winapi

    # Ctrl+Break reaches only a process of its own group.
    NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    NEW_PROCESS_GROUP = 0

# The rules that exist only for Unix files and sockets; the protocol contract has no such skip.
UNIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a Unix socket path limit and POSIX file modes; Windows uses a pipe name and a DACL",
)

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 64
HEADER = struct.Struct(">I")
# A socket path is limited to 100 bytes, which a pytest or CI temporary directory can exceed. A
# writable /tmp is short; without one the temporary directory of the environment (TMPDIR) is used.
SHORT_TMP = (
    "/tmp"
    if sys.platform != "win32" and os.access("/tmp", os.W_OK | os.X_OK)
    else tempfile.gettempdir()
)


class Directories:
    """A repository and a runtime directory, both under a path short enough for a socket."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="hr", dir=SHORT_TMP)).resolve()
        self.repo = self.base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.xdg = self.base / "run"
        self.xdg.mkdir(mode=0o700)

    @property
    def environ(self) -> dict[str, str]:
        return {"XDG_RUNTIME_DIR": str(self.xdg)}


@pytest.fixture
def dirs() -> Iterator[Directories]:
    directories = Directories()
    yield directories
    shutil.rmtree(directories.base, ignore_errors=True)


Connection = runtime_client._Transport  # a Unix socket or a named pipe end


def _read(connection: Connection, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _read_frame(connection: Connection) -> JsonObject:
    (length,) = HEADER.unpack(_read(connection, HEADER.size))
    loaded: JsonObject = json.loads(_read(connection, length))
    return loaded


def _frame(message: JsonObject) -> bytes:
    body = json.dumps(message).encode()
    return HEADER.pack(len(body)) + body


def _write_frame(connection: Connection, message: JsonObject) -> None:
    connection.sendall(_frame(message))


def _ok(operation_id: object, result: JsonObject) -> JsonObject:
    return {
        "operation_id": operation_id,
        "status": "ok",
        "revision": None,
        "result": result,
    }


def _error(operation_id: object, code: str, **extra: object) -> JsonObject:
    error: JsonObject = {
        "code": code,
        "domain": "policy",
        "retryable": False,
        "message": f"{code} happened",
        "remedy": f"fix {code}",
        "facts": {"seen": True},
    }
    error.update(extra)
    return {
        "operation_id": operation_id,
        "status": "error",
        "revision": None,
        "error": error,
    }


HELLO_RESULT: JsonObject = {
    "protocol": {"major": 1, "minor": 0},
    "server_version": "0.1.0",
    "store_id": None,
    "features": ["status"],
}


class Listener(Protocol):
    """The listening end of a scripted runtime."""

    def accept(self) -> Connection:
        """Wait for a client; raise ``OSError`` once the listener is closed."""

    def close(self) -> None:
        """Stop listening."""


if sys.platform != "win32":

    class _SocketListener:
        """The Unix socket at which a scripted runtime listens."""

        def __init__(self, address: str) -> None:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.bind(address)
            self.socket.listen()

        def accept(self) -> Connection:
            connection, _ = self.socket.accept()
            return connection

        def close(self) -> None:
            self.socket.close()


class _ListenerApi(runtime_client._PipeApi, Protocol):
    """The server half of ``_winapi`` that the scripted runtime uses besides the client half."""

    PIPE_ACCESS_DUPLEX: int
    FILE_FLAG_FIRST_PIPE_INSTANCE: int
    PIPE_WAIT: int
    PIPE_UNLIMITED_INSTANCES: int
    NMPWAIT_WAIT_FOREVER: int

    def CreateNamedPipe(
        self,
        name: str,
        open_mode: int,
        pipe_mode: int,
        max_instances: int,
        out_buffer_size: int,
        in_buffer_size: int,
        default_timeout: int,
        security_attributes: int,
        /,
    ) -> int:
        """Create an instance of the pipe."""

    def ConnectNamedPipe(
        self, handle: int, /, overlapped: bool = False
    ) -> runtime_client._Overlapped:
        """Start the wait for a client at an instance."""


class _PipeListener:
    """The named pipe at which a scripted runtime listens: one instance per connection.

    The Win32 API is a parameter, as for the pipe of the client: the real ``_winapi`` on Windows,
    a scripted fake wherever the rules of the listener are checked on another platform.
    """

    def __init__(self, name: str, api: _ListenerApi | None = None) -> None:
        self.api = cast(_ListenerApi, runtime_client._windows_pipe_api()) if api is None else api
        self.name = name
        self.closed = threading.Event()
        self.instance = self._create(first=True)

    def _create(self, *, first: bool) -> int:
        api = self.api
        flags = api.PIPE_ACCESS_DUPLEX | api.FILE_FLAG_OVERLAPPED
        if first:
            flags |= api.FILE_FLAG_FIRST_PIPE_INSTANCE
        handle: int = api.CreateNamedPipe(
            self.name,
            flags,
            # Byte type and byte read mode are both 0, the default.
            api.PIPE_WAIT,
            api.PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            api.NMPWAIT_WAIT_FOREVER,
            api.NULL,
        )
        return handle

    def accept(self) -> Connection:
        api = self.api
        instance = self.instance
        overlapped = api.ConnectNamedPipe(instance, overlapped=True)
        try:
            while api.WaitForMultipleObjects([overlapped.event], False, 50) != 0:
                if self.closed.is_set():
                    raise OSError("the listener is closed")
        except BaseException:
            # The cancelled connect ends at once; only then is the handle closed.
            overlapped.cancel()
            overlapped.GetOverlappedResult(True)
            api.CloseHandle(instance)
            raise
        overlapped.GetOverlappedResult(True)
        self.instance = self._create(first=False)
        return runtime_client._PipeConnection(instance, lambda: 10.0, api)

    def close(self) -> None:
        self.closed.set()


if sys.platform == "win32":

    def _listen(address: Path) -> Listener:
        return _PipeListener(str(address))

else:

    def _listen(address: Path) -> Listener:
        return _SocketListener(str(address))


class ScriptedRuntime:
    """A runtime at the socket of a repository: it speaks the protocol and records every frame."""

    def __init__(
        self,
        directories: Directories,
        *,
        drop_connections: int = 0,
        answer: Callable[[JsonObject], JsonObject] | None = None,
    ) -> None:
        self.paths = runtime_client.runtime_paths(directories.repo, directories.environ)
        self.drop_connections = drop_connections
        self.answer = answer
        self.requests: list[JsonObject] = []
        self.connections = 0
        self.paths.directory.mkdir(parents=True, mode=0o700)
        self.paths.token.write_text(TOKEN)
        self.listener = _listen(self.paths.socket)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.listener.close()

    def _accept(self) -> None:
        while True:
            try:
                connection = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            if self.connections <= self.drop_connections:
                connection.close()
                continue
            threading.Thread(
                target=self._serve, args=(connection,), daemon=True
            ).start()

    def _serve(self, connection: Connection) -> None:
        with closing(connection):
            try:
                while True:
                    request = _read_frame(connection)
                    self.requests.append(request)
                    if request["operation"] == "hello":
                        payload = request["payload"]
                        if payload["token"] != TOKEN:
                            _write_frame(
                                connection,
                                _error(request["operation_id"], "unauthorized_peer"),
                            )
                            return
                        if (
                            self.answer is not None
                            and (refusal := self.answer(request)).get("status")
                            == "error"
                        ):
                            _write_frame(connection, refusal)
                            return
                        _write_frame(
                            connection, _ok(request["operation_id"], HELLO_RESULT)
                        )
                    elif self.answer is not None:
                        _write_frame(connection, self.answer(request))
                    else:
                        _write_frame(
                            connection,
                            _ok(request["operation_id"], {"state": "serving"}),
                        )
            except (ConnectionError, OSError):
                return

    def operations(self, name: str) -> list[JsonObject]:
        return [request for request in self.requests if request["operation"] == name]


@pytest.fixture
def runtimes() -> Iterator[list[ScriptedRuntime]]:
    started: list[ScriptedRuntime] = []
    yield started
    for runtime in started:
        runtime.close()


class FakeTime:
    """A clock that only the replaced sleep advances."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# -- the address ---------------------------------------------------------------------------------


def test_the_repo_id_is_the_shared_golden_vector() -> None:
    # The Rust test `the_repo_id_is_sixteen_hex_digits_of_the_sha256_of_the_path` holds the same pair.
    assert runtime_client.repo_id(PurePosixPath("/r/.git")) == "2d09593ea5e01029"


def test_the_windows_repo_id_is_the_shared_golden_vector() -> None:
    # The Rust test `the_repo_id_of_a_windows_path_is_the_same_on_every_platform` holds the same pair.
    assert runtime_client.repo_id(PureWindowsPath(r"C:\r\.git")) == "34fdb197a11ee020"


def test_the_windows_pipe_name_is_the_shared_golden_vector() -> None:
    # The Rust test `the_windows_pipe_is_named_after_the_repo_id` holds the same pair.
    assert (
        runtime_client.pipe_name("34fdb197a11ee020")
        == r"\\.\pipe\harness-runtime-34fdb197a11ee020"
    )


def test_on_windows_the_address_is_a_named_pipe_without_the_unix_path_limit(
    dirs: Directories,
) -> None:
    long_xdg = "C:\\run\\" + "x" * 100

    paths = runtime_client.runtime_paths(
        dirs.repo, {"XDG_RUNTIME_DIR": long_xdg}, platform="win32"
    )

    identity = runtime_client.repo_id(dirs.repo / ".git")
    assert str(paths.socket) == rf"\\.\pipe\harness-runtime-{identity}"
    assert paths.directory == Path(long_xdg + "\\harness")
    assert paths.token == Path(long_xdg + "\\harness") / f"{identity}.token"


def test_on_windows_without_an_absolute_directory_there_is_no_address(
    dirs: Directories,
) -> None:
    with pytest.raises(RuntimeClientError) as caught:
        runtime_client.runtime_paths(
            dirs.repo, {"XDG_RUNTIME_DIR": "/run/user/1000"}, platform="win32"
        )

    assert caught.value.code == "runtime_unavailable"
    assert "LOCALAPPDATA" in caught.value.remedy


@UNIX_ONLY
def test_the_socket_and_the_token_sit_in_the_xdg_runtime_directory(
    dirs: Directories,
) -> None:
    paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)

    identity = runtime_client.repo_id(dirs.repo / ".git")
    assert paths.directory == dirs.xdg / "harness"
    assert paths.socket == dirs.xdg / "harness" / f"{identity}.sock"
    assert paths.token == dirs.xdg / "harness" / f"{identity}.token"


@UNIX_ONLY
def test_without_xdg_the_runtime_directory_lies_outside_the_repository(
    dirs: Directories,
) -> None:
    for environ in ({}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "relative/run"}):
        paths = runtime_client.runtime_paths(dirs.repo, environ)

        assert paths.directory == Path(f"/tmp/harness-{os.getuid()}")
        assert dirs.repo not in paths.directory.parents


def test_the_runtime_directory_follows_the_fixture_the_runtime_passes() -> None:
    fixture = json.loads(
        (
            ROOT / "native/harness-runtime/crates/ipc/tests/fixtures/runtime_paths.json"
        ).read_text(encoding="utf-8")
    )
    assert len(fixture["cases"]) >= 10
    for case in fixture["cases"]:
        chosen = runtime_client.runtime_directory(
            "win32" if case["platform"] == "windows" else "linux",
            case["xdg_runtime_dir"],
            case["local_app_data"],
            case["uid"],
        )

        assert chosen == case["directory"], case["name"]


def test_a_linked_worktree_shares_the_runtime_of_its_main_checkout(
    dirs: Directories,
) -> None:
    admin = dirs.repo / ".git/worktrees/w"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    worktree = dirs.base / "w"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n")

    assert runtime_client.runtime_paths(
        worktree, dirs.environ
    ) == runtime_client.runtime_paths(dirs.repo, dirs.environ)


def test_a_directory_that_is_no_repository_has_no_address(dirs: Directories) -> None:
    with pytest.raises(RuntimeClientError) as caught:
        runtime_client.runtime_paths(dirs.base, dirs.environ)

    assert caught.value.code == "runtime_unavailable"


@UNIX_ONLY
def test_a_socket_path_over_the_limit_is_refused(dirs: Directories) -> None:
    environ = {"XDG_RUNTIME_DIR": str(dirs.xdg / ("x" * 100))}

    with pytest.raises(RuntimeClientError) as caught:
        runtime_client.runtime_paths(dirs.repo, environ)

    assert caught.value.code == "runtime_unavailable"
    assert "100 bytes" in caught.value.message


# -- the request ---------------------------------------------------------------------------------


def test_a_request_says_hello_with_the_token_then_asks(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtime = ScriptedRuntime(dirs)
    runtimes.append(runtime)

    reply = RuntimeClient(dirs.repo, environ=dirs.environ).call(
        "status", operation_id="op-1"
    )

    assert reply.result == {"state": "serving"}
    assert reply.hello["server_version"] == "0.1.0"
    (hello,) = runtime.operations("hello")
    assert hello["payload"]["token"] == TOKEN
    assert hello["payload"]["protocol"] == {
        "min": {"major": 1, "minor": 5},
        "max": {"major": 1, "minor": 5},
    }
    assert hello["payload"]["repo_root"] == str(dirs.repo)
    (status,) = runtime.operations("status")
    assert status["operation_id"] == "op-1"
    assert status["protocol_version"] == {"major": 1, "minor": 5}


def test_a_lost_transport_repeats_the_request_with_the_same_operation_id(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtime = ScriptedRuntime(dirs, drop_connections=2)
    runtimes.append(runtime)
    timing = FakeTime()
    client = RuntimeClient(
        dirs.repo, environ=dirs.environ, clock=timing.clock, sleep=timing.sleep
    )

    reply = client.call("status", operation_id="same-intent", timeout_seconds=60)

    assert reply.result == {"state": "serving"}
    assert runtime.connections == 3
    assert [request["operation_id"] for request in runtime.operations("status")] == [
        "same-intent"
    ]
    assert timing.sleeps == [0.1, 0.2]


def test_every_attempt_keeps_the_first_request_deadline(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtime = ScriptedRuntime(dirs)
    runtimes.append(runtime)
    timing = FakeTime()
    client = RuntimeClient(dirs.repo, environ=dirs.environ, clock=timing.clock)

    client.call("status", operation_id="a", timeout_seconds=30)

    (status,) = runtime.operations("status")
    assert status["request_deadline"] == int((timing.now + 30) * 1000)


def test_the_delay_doubles_up_to_five_seconds(dirs: Directories) -> None:
    timing = FakeTime()
    client = RuntimeClient(
        dirs.repo, environ=dirs.environ, clock=timing.clock, sleep=timing.sleep
    )

    with pytest.raises(RuntimeClientError):
        client.call("status", operation_id="x", timeout_seconds=40)

    assert timing.sleeps[:7] == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0]
    assert max(timing.sleeps) == 5.0
    assert sum(timing.sleeps) == pytest.approx(40)


def test_an_unreachable_runtime_is_runtime_unavailable_and_writes_nothing(
    dirs: Directories,
) -> None:
    timing = FakeTime()
    client = RuntimeClient(
        dirs.repo, environ=dirs.environ, clock=timing.clock, sleep=timing.sleep
    )
    before = sorted(path for path in dirs.base.rglob("*"))

    with pytest.raises(RuntimeClientError) as caught:
        client.call("status", operation_id="x", timeout_seconds=3)

    error = caught.value
    assert error.code == "runtime_unavailable"
    assert error.retryable
    assert error.remedy.startswith("[runtime_unavailable] ")
    assert "never writes state without it" in error.remedy
    assert sorted(dirs.base.rglob("*")) == before
    assert not (dirs.repo / ".harness").exists()


def test_a_windows_connect_gets_the_real_time_left_so_a_busy_pipe_can_time_out(
    dirs: Directories, monkeypatch: pytest.MonkeyPatch
) -> None:
    timing = FakeTime()
    client = RuntimeClient(
        dirs.repo, environ=dirs.environ, clock=timing.clock, sleep=timing.sleep
    )
    given: list[Callable[[], float | None]] = []

    class RecordingPipe:
        @classmethod
        def open(
            cls, name: str, remaining: Callable[[], float | None]
        ) -> RecordingPipe:
            given.append(remaining)
            raise OSError("the pipe stays busy")

    monkeypatch.setattr(runtime_client, "_PipeConnection", RecordingPipe)
    monkeypatch.setattr(sys, "platform", "win32")
    deadline = timing.now + 1.0

    with pytest.raises(OSError, match="busy"):
        client._connect(deadline)
    timing.now = deadline + 5.0

    # A floor above zero would hide the passed deadline from the wait loop of the pipe.
    limit = given[0]()
    assert limit is not None
    assert limit <= 0


# -- the Windows pipe through a fake of the _winapi boundary --------------------------------------
# These run on every platform: the pipe takes its Win32 API as a parameter, so a scripted fake
# stands in for ``_winapi`` and each rule of the transport is observed at that boundary.

ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_SEM_TIMEOUT = 121
ERROR_ACCESS_DENIED = 5
SQOS_IDENTIFICATION = 0x00100000 | 0x00010000


class WinError(OSError):
    """An ``OSError`` with ``winerror``, which only Windows fills in."""

    def __init__(self, winerror: int) -> None:
        super().__init__(f"winerror {winerror}")
        self.winerror = winerror


class FakeOverlapped:
    """A started operation; a cancelled one ends as aborted (995)."""

    event = 99

    def __init__(self, moved: bytes) -> None:
        self.moved = moved
        self.cancelled = False

    def GetOverlappedResult(self, wait: bool, /) -> tuple[int, int]:
        return (0, 995) if self.cancelled else (len(self.moved), 0)

    def cancel(self) -> None:
        self.cancelled = True

    def getbuffer(self) -> bytes | None:
        return self.moved


class FakePipeApi:
    """A scripted ``_winapi``: it records its calls and fails or stalls where a test says so.

    With ``pending`` every read and write is still running when it starts. ``signalled`` says
    whether its event is set when the pipe waits for it; an unset one runs into the time limit.
    """

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    NULL = 0
    OPEN_EXISTING = 3
    FILE_FLAG_OVERLAPPED = 0x40000000
    INFINITE = 0xFFFFFFFF
    HANDLE = 7
    PIPE_ACCESS_DUPLEX = 3
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_WAIT = 0
    PIPE_UNLIMITED_INSTANCES = 255
    NMPWAIT_WAIT_FOREVER = 0xFFFFFFFF

    def __init__(
        self,
        *,
        busy: int = 0,
        incoming: bytes = b"",
        write_limit: int = 1 << 20,
        pending: bool = False,
        signalled: bool = True,
        create_error: OSError | None = None,
        named_wait_error: OSError | None = None,
        event_wait_error: OSError | None = None,
        read_error: OSError | None = None,
        timing: FakeTime | None = None,
    ) -> None:
        self.busy = busy
        self.incoming = bytearray(incoming)
        self.write_limit = write_limit
        self.pending = pending
        self.signalled = signalled
        self.create_error = create_error
        self.named_wait_error = named_wait_error
        self.event_wait_error = event_wait_error
        self.read_error = read_error
        self.timing = timing
        self.written = bytearray()
        self.create_calls: list[tuple[object, ...]] = []
        self.named_waits: list[int] = []
        self.event_waits: list[int] = []
        self.operations: list[FakeOverlapped] = []
        self.closed: list[int] = []
        self.pipes: list[tuple[object, ...]] = []
        self.connects: list[FakeOverlapped] = []

    def CreateNamedPipe(self, *args: object) -> int:
        self.pipes.append(args)
        return 100 + len(self.pipes)

    def ConnectNamedPipe(self, handle: int, /, overlapped: bool = False) -> FakeOverlapped:
        operation = FakeOverlapped(b"")
        self.connects.append(operation)
        return operation

    def CreateFile(self, *args: object) -> int:
        self.create_calls.append(args)
        if self.create_error is not None:
            raise self.create_error
        if self.busy:
            self.busy -= 1
            raise WinError(ERROR_PIPE_BUSY)
        return self.HANDLE

    def WaitNamedPipe(self, name: str, timeout: int, /) -> None:
        self.named_waits.append(timeout)
        if self.timing is not None:
            self.timing.sleep(timeout / 1000)
        if self.named_wait_error is not None:
            raise self.named_wait_error

    def _start(self, moved: bytes) -> tuple[FakeOverlapped, int]:
        operation = FakeOverlapped(moved)
        self.operations.append(operation)
        return operation, (997 if self.pending else 0)

    def WriteFile(
        self, handle: int, buffer: bytes | memoryview, /, overlapped: bool = False
    ) -> tuple[FakeOverlapped, int]:
        chunk = bytes(buffer[: self.write_limit])
        if self.signalled or not self.pending:
            self.written.extend(chunk)
        return self._start(chunk)

    def ReadFile(
        self, handle: int, size: int, /, overlapped: bool = False
    ) -> tuple[FakeOverlapped, int]:
        if self.read_error is not None:
            raise self.read_error
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return self._start(data)

    def WaitForMultipleObjects(
        self, handles: list[int], wait_all: bool, milliseconds: int, /
    ) -> int:
        self.event_waits.append(milliseconds)
        if self.event_wait_error is not None:
            raise self.event_wait_error
        return 0 if self.signalled else 258

    def CloseHandle(self, handle: int, /) -> None:
        self.closed.append(handle)


def _frames(data: bytes) -> list[JsonObject]:
    frames: list[JsonObject] = []
    while data:
        (length,) = HEADER.unpack_from(data)
        frames.append(json.loads(data[HEADER.size : HEADER.size + length]))
        data = data[HEADER.size + length :]
    return frames


def _pipe(api: FakePipeApi, limit: float | None = 10.0) -> runtime_client._PipeConnection:
    return runtime_client._PipeConnection(api.HANDLE, lambda: limit, api)


@pytest.mark.parametrize("pending", [False, True])
def test_a_windows_request_says_hello_then_asks_through_the_pipe_api(
    dirs: Directories, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)
    paths.directory.mkdir(parents=True, mode=0o700)
    paths.token.write_text(TOKEN)
    api = FakePipeApi(
        incoming=_frame(_ok("op-1.hello", HELLO_RESULT))
        + _frame(_ok("op-1", {"state": "serving"})),
        pending=pending,
    )
    client = RuntimeClient(dirs.repo, environ=dirs.environ)
    # Without an `api` the pipe takes the real `_winapi` module, here the fake.
    monkeypatch.setitem(sys.modules, "_winapi", api)
    monkeypatch.setattr(sys, "platform", "win32")

    reply = client.call("status", operation_id="op-1")

    assert reply.result == {"state": "serving"}
    hello, status = _frames(bytes(api.written))
    assert hello["payload"]["token"] == TOKEN
    assert status["operation_id"] == "op-1"
    (create,) = api.create_calls
    assert create[0] == str(client.paths.socket)
    # The client lets the server identify it but not impersonate it.
    assert create[5] == api.FILE_FLAG_OVERLAPPED | SQOS_IDENTIFICATION
    assert api.closed == [api.HANDLE]
    assert (api.event_waits != []) == pending
    assert all(milliseconds > 0 for milliseconds in api.event_waits)


def test_a_token_without_a_listening_runtime_is_runtime_unavailable(
    dirs: Directories,
) -> None:
    paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)
    paths.directory.mkdir(parents=True, mode=0o700)
    paths.token.write_text(TOKEN)
    timing = FakeTime()
    client = RuntimeClient(
        dirs.repo, environ=dirs.environ, clock=timing.clock, sleep=timing.sleep
    )

    with pytest.raises(RuntimeClientError) as caught:
        client.call("status", operation_id="x", timeout_seconds=1)

    assert caught.value.code == "runtime_unavailable"
    assert timing.sleeps


def test_a_busy_pipe_is_waited_for_until_an_instance_is_free() -> None:
    timing = FakeTime()
    api = FakePipeApi(busy=2, timing=timing)
    deadline = timing.now + 10

    pipe = runtime_client._PipeConnection.open(
        "pipe", lambda: deadline - timing.clock(), api
    )

    assert api.named_waits == [500, 500]
    assert len(api.create_calls) == 3
    pipe.close()


def test_the_wait_for_a_busy_pipe_never_outlasts_the_time_left() -> None:
    timing = FakeTime()
    api = FakePipeApi(busy=1, timing=timing)
    deadline = timing.now + 0.25

    runtime_client._PipeConnection.open("pipe", lambda: deadline - timing.clock(), api)

    assert api.named_waits == [250]


def test_a_pipe_that_stays_busy_past_the_limit_is_a_timeout() -> None:
    timing = FakeTime()
    api = FakePipeApi(busy=10**6, timing=timing)
    deadline = timing.now + 1.25

    with pytest.raises(TimeoutError, match="stayed busy"):
        runtime_client._PipeConnection.open(
            "pipe", lambda: deadline - timing.clock(), api
        )

    assert api.named_waits == [500, 500, 250]


def test_a_busy_pipe_without_a_limit_is_waited_for_in_slices() -> None:
    api = FakePipeApi(busy=1)

    runtime_client._PipeConnection.open("pipe", lambda: None, api)

    assert api.named_waits == [500]


def test_a_wait_for_a_busy_pipe_that_times_out_looks_again() -> None:
    api = FakePipeApi(busy=1, named_wait_error=WinError(ERROR_SEM_TIMEOUT))

    runtime_client._PipeConnection.open("pipe", lambda: 10.0, api)

    assert len(api.create_calls) == 2


def test_a_failed_wait_for_a_busy_pipe_is_raised() -> None:
    api = FakePipeApi(busy=1, named_wait_error=WinError(ERROR_ACCESS_DENIED))

    with pytest.raises(OSError, match="winerror 5"):
        runtime_client._PipeConnection.open("pipe", lambda: 10.0, api)


def test_an_open_error_other_than_busy_is_raised_at_once() -> None:
    api = FakePipeApi(create_error=WinError(2))

    with pytest.raises(OSError, match="winerror 2"):
        runtime_client._PipeConnection.open("pipe", lambda: 10.0, api)

    assert api.named_waits == []


def test_a_write_in_parts_sends_every_byte() -> None:
    api = FakePipeApi(write_limit=3)

    _pipe(api).sendall(b"0123456789")

    assert bytes(api.written) == b"0123456789"
    assert len(api.operations) == 4


def test_a_write_that_does_not_end_by_the_limit_is_cancelled_as_a_timeout() -> None:
    api = FakePipeApi(pending=True, signalled=False)

    with pytest.raises(TimeoutError, match="take the request"):
        _pipe(api).sendall(b"request")

    assert api.operations[0].cancelled


def test_a_read_gives_what_the_runtime_sent_in_pieces() -> None:
    pipe = _pipe(FakePipeApi(incoming=b"abcdef"))

    assert (pipe.recv(4), pipe.recv(4)) == (b"abcd", b"ef")


def test_a_read_that_does_not_end_by_the_limit_is_cancelled_as_a_timeout() -> None:
    api = FakePipeApi(pending=True, signalled=False)

    with pytest.raises(TimeoutError, match="answer in time"):
        _pipe(api).recv(4)

    assert api.operations[0].cancelled


def test_a_passed_limit_cancels_an_operation_without_waiting() -> None:
    api = FakePipeApi(pending=True)

    with pytest.raises(TimeoutError):
        _pipe(api, limit=0.0).recv(4)

    assert api.event_waits == []
    assert api.operations[0].cancelled


def test_an_operation_without_a_limit_waits_without_end() -> None:
    api = FakePipeApi(incoming=b"ok", pending=True)

    assert _pipe(api, limit=None).recv(2) == b"ok"

    assert api.event_waits == [api.INFINITE]


def test_a_broken_pipe_reads_as_a_closed_connection() -> None:
    api = FakePipeApi(read_error=WinError(ERROR_BROKEN_PIPE))

    assert _pipe(api).recv(4) == b""


def test_a_read_error_other_than_a_broken_pipe_is_raised() -> None:
    api = FakePipeApi(read_error=WinError(ERROR_ACCESS_DENIED))

    with pytest.raises(OSError, match="winerror 5"):
        _pipe(api).recv(4)


def test_a_failed_wait_cancels_the_operation_and_is_raised() -> None:
    api = FakePipeApi(pending=True, event_wait_error=OSError("the wait failed"))

    with pytest.raises(OSError, match="the wait failed"):
        _pipe(api).recv(4)

    assert api.operations[0].cancelled


def test_a_closed_pipe_refuses_io_and_closing_twice_is_harmless() -> None:
    api = FakePipeApi()
    pipe = _pipe(api)

    pipe.close()
    pipe.close()

    assert api.closed == [api.HANDLE]
    with pytest.raises(OSError, match="closed"):
        pipe.sendall(b"x")
    with pytest.raises(OSError, match="closed"):
        pipe.recv(1)


def test_leaving_the_context_closes_the_pipe() -> None:
    api = FakePipeApi()

    with _pipe(api):
        assert api.closed == []

    assert api.closed == [api.HANDLE]


def test_a_pipe_listener_serves_one_instance_per_client_and_opens_the_next() -> None:
    api = FakePipeApi()
    listener = _PipeListener(r"\\.\pipe\scripted", api)

    first = listener.instance
    connection = listener.accept()

    # Only the first instance claims the name; the next is created at once for the next client.
    (opened_first, opened_next) = api.pipes
    assert opened_first[0] == opened_next[0] == r"\\.\pipe\scripted"
    first_flags = opened_first[1]
    assert isinstance(first_flags, int)
    assert first_flags & api.FILE_FLAG_FIRST_PIPE_INSTANCE
    assert first_flags & api.FILE_FLAG_OVERLAPPED
    next_flags = opened_next[1]
    assert isinstance(next_flags, int)
    assert not next_flags & api.FILE_FLAG_FIRST_PIPE_INSTANCE
    assert listener.instance != first
    # The client end of the accepted instance talks through the same API.
    connection.sendall(b"hi")
    assert bytes(api.written) == b"hi"
    connection.close()
    assert api.closed == [first]


def test_a_closed_pipe_listener_cancels_the_connect_and_frees_the_instance() -> None:
    api = FakePipeApi(signalled=False)
    listener = _PipeListener(r"\\.\pipe\scripted", api)
    instance = listener.instance
    listener.close()

    with pytest.raises(OSError, match="closed"):
        listener.accept()

    (connect,) = api.connects
    assert connect.cancelled
    assert api.closed == [instance]
    assert len(api.pipes) == 1


@pytest.mark.parametrize(
    ("error", "exists"),
    [
        (None, True),
        (WinError(2), False),
        (WinError(ERROR_SEM_TIMEOUT), True),
        (OSError("not a Windows error"), True),
    ],
)
def test_a_pipe_exists_unless_its_name_is_not_found(
    error: OSError | None, exists: bool
) -> None:
    api = FakePipeApi(named_wait_error=error)

    assert _pipe_exists(Path(r"\\.\pipe\scripted"), api) is exists
    assert api.named_waits == [1]


if sys.platform == "win32":

    def test_a_pipe_whose_only_instance_is_taken_ends_in_runtime_unavailable(
        dirs: Directories,
    ) -> None:
        paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)
        paths.directory.mkdir(parents=True, mode=0o700)
        paths.token.write_text(TOKEN)
        pipe = _PipeListener(str(paths.socket))
        connecting = _winapi.ConnectNamedPipe(pipe.instance, overlapped=True)
        # One client takes the only instance and the scripted runtime never accepts another.
        occupant = _winapi.CreateFile(
            str(paths.socket),
            _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0,
            _winapi.NULL,
            _winapi.OPEN_EXISTING,
            _winapi.FILE_FLAG_OVERLAPPED,
            _winapi.NULL,
        )
        client = RuntimeClient(dirs.repo, environ=dirs.environ)
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeClientError) as caught:
                client.call("status", operation_id="busy", timeout_seconds=1.0)
            elapsed = time.monotonic() - started
        finally:
            _winapi.CloseHandle(occupant)
            connecting.GetOverlappedResult(True)
            _winapi.CloseHandle(pipe.instance)

        assert caught.value.code == "runtime_unavailable"
        assert elapsed < 10.0


@pytest.mark.parametrize(
    ("code", "domain", "retryable"),
    [
        ("revision_conflict", "policy", False),
        ("store_busy", "infrastructure", True),
        ("protocol_unsupported", "tooling", False),
    ],
)
def test_an_error_of_the_runtime_is_typed_and_not_repeated(
    dirs: Directories,
    runtimes: list[ScriptedRuntime],
    code: str,
    domain: str,
    retryable: bool,
) -> None:
    runtime = ScriptedRuntime(
        dirs,
        answer=lambda request: _error(
            request["operation_id"], code, domain=domain, retryable=retryable
        ),
    )
    runtimes.append(runtime)

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="e")

    error = caught.value
    assert (error.code, error.domain, error.retryable) == (code, domain, retryable)
    assert error.facts == {"seen": True}
    assert error.message == f"{code} happened"
    assert error.remedy == f"[{code}] fix {code}"
    # A refused hello is the first frame; a refused request would be the second.
    assert runtime.connections == 1


def test_a_code_the_client_does_not_know_is_internal(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtimes.append(
        ScriptedRuntime(
            dirs,
            answer=lambda request: _error(request["operation_id"], "from_the_future"),
        )
    )

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="u")

    assert caught.value.code == "internal"
    assert "from_the_future" in caught.value.message


def test_a_field_the_client_does_not_know_is_ignored(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    def answer(request: JsonObject) -> JsonObject:
        return {
            **_ok(request["operation_id"], {"state": "serving"}),
            "added_later": [1],
        }

    runtimes.append(ScriptedRuntime(dirs, answer=answer))

    reply = RuntimeClient(dirs.repo, environ=dirs.environ).call(
        "status", operation_id="f"
    )

    assert reply.result == {"state": "serving"}


def test_a_wrong_token_is_unauthorized_peer(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtime = ScriptedRuntime(dirs)
    runtimes.append(runtime)
    runtime.paths.token.write_text("someone-elses-token")

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="w")

    assert caught.value.code == "unauthorized_peer"


@UNIX_ONLY
def test_a_runtime_directory_open_to_others_or_a_symlink_is_never_trusted(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    runtime = ScriptedRuntime(dirs)
    runtimes.append(runtime)
    directory = runtime.paths.directory
    directory.chmod(0o755)

    with pytest.raises(RuntimeClientError) as opened:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="o")
    directory.chmod(0o700)
    moved = directory.with_name("elsewhere")
    directory.rename(moved)
    directory.symlink_to(moved)
    with pytest.raises(RuntimeClientError) as linked:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="l")

    assert (opened.value.code, linked.value.code) == ("unauthorized_peer",) * 2
    assert "open to the group or to others" in opened.value.message
    assert "symbolic link" in linked.value.message
    assert "XDG_RUNTIME_DIR" in linked.value.remedy
    assert runtime.requests == []


@UNIX_ONLY
def test_a_platform_without_unix_sockets_is_runtime_unavailable(
    dirs: Directories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(socket, "AF_UNIX")

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="x")

    assert caught.value.code == "runtime_unavailable"
    assert "Unix domain sockets" in caught.value.message


def test_the_module_imports_where_the_socket_module_has_no_unix_apis() -> None:
    script = (
        "import socket, os\n"
        "if hasattr(socket, 'AF_UNIX'):\n"
        "    del socket.AF_UNIX\n"
        "if hasattr(os, 'getuid'):\n"
        "    del os.getuid\n"
        "import harness.orchestration.runtime_client\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


# -- the command ---------------------------------------------------------------------------------


def _run_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    directories: Directories,
    *extra: str,
) -> tuple[int, str, str]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(directories.xdg))
    monkeypatch.setattr(
        sys,
        "argv",
        ["coordinator", "runtime", "status", "--repo", str(directories.repo), *extra],
    )
    code = coordinator.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_runtime_status_prints_what_the_runtime_reported(
    dirs: Directories,
    runtimes: list[ScriptedRuntime],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtimes.append(ScriptedRuntime(dirs))

    code, out, _ = _run_command(monkeypatch, capsys, dirs)

    assert code == 0
    report = json.loads(out)
    assert report["server_version"] == "0.1.0"
    assert report["protocol"] == {"major": 1, "minor": 0}
    assert report["runtime"] == {"state": "serving"}
    assert report["socket"] == str(runtimes[0].paths.socket)


def test_an_unreachable_runtime_exits_2_with_the_code_in_the_remedy(
    dirs: Directories,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run_command(monkeypatch, capsys, dirs, "--timeout", "0.2")

    assert code == 2
    assert out == ""
    lines = err.splitlines()
    assert lines[0].startswith("ERROR: harness-runtime does not answer on ")
    assert lines[1].startswith("REMEDY: [runtime_unavailable] ")


def test_an_error_of_the_runtime_exits_2_with_its_code_in_the_remedy(
    dirs: Directories,
    runtimes: list[ScriptedRuntime],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtimes.append(
        ScriptedRuntime(
            dirs,
            answer=lambda request: _error(
                request["operation_id"], "protocol_unsupported"
            ),
        )
    )

    code, _, err = _run_command(monkeypatch, capsys, dirs)

    assert code == 2
    assert (
        err.splitlines()[1] == "REMEDY: [protocol_unsupported] fix protocol_unsupported"
    )


def test_a_busy_store_read_prints_the_store_busy_code_first(
    dirs: Directories,
    runtimes: list[ScriptedRuntime],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(dirs.xdg))
    runtimes.append(
        ScriptedRuntime(
            dirs,
            answer=lambda request: _error(request["operation_id"], "store_busy"),
        )
    )
    fetch = ledger_ops._runtime_fetch(dirs.repo)

    with pytest.raises(ledger_ops.LedgerBusyError) as busy:
        fetch(["batches"], [], None)

    code = errors.print_and_exit(busy.value)

    assert code == 2
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "ERROR: store_busy happened"
    assert lines[1] == "REMEDY: [store_busy] fix store_busy"


# -- seam 1: the coordinator CLI against a real ``harness-runtime serve`` -------------------------


def _runtime_binary() -> Path:
    configured = os.environ.get("HARNESS_RUNTIME_BIN")
    name = "harness-runtime.exe" if sys.platform == "win32" else "harness-runtime"
    binary = (
        Path(configured)
        if configured
        else ROOT / "native/harness-runtime/target/debug" / name
    )
    if binary.is_file():
        return binary
    message = (
        f"build harness-runtime first (cargo build in native/harness-runtime): {binary}"
    )
    if os.environ.get("HARNESS_REQUIRE_RUNTIME") == "1":
        pytest.fail(message)
    pytest.skip(message)


class RuntimeProcess:
    """One ``harness-runtime serve`` process of a repository."""

    def __init__(self, directories: Directories, repo: Path | None = None) -> None:
        self.directories = directories
        self.process = subprocess.Popen(
            [str(_runtime_binary()), "serve", "--repo", str(repo or directories.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "XDG_RUNTIME_DIR": str(directories.xdg)},
            creationflags=NEW_PROCESS_GROUP,
        )
        self.paths = runtime_client.runtime_paths(directories.repo, directories.environ)

    def wait_until_ready(self) -> dict[str, str]:
        stdout = self.process.stdout
        assert stdout is not None
        # A thread reads the line: `select` does not work on a pipe on Windows.
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=lambda: lines.put(stdout.readline()), daemon=True
        ).start()
        try:
            text = lines.get(timeout=20)
        except queue.Empty:
            pytest.fail("harness-runtime printed no readiness line within 20 seconds")
        line: dict[str, str] = json.loads(text)
        assert line["status"] == "ready"
        return line

    def stop(self, *, hard: bool = False) -> int:
        """End the process: SIGTERM, or Ctrl+Break on Windows; ``hard`` kills it."""
        if hard:
            self.process.kill()
        elif sys.platform == "win32":
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.process.send_signal(signal.SIGTERM)
        code = self.process.wait(timeout=20)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()
        return code


@pytest.fixture
def real_runtime(dirs: Directories) -> Iterator[RuntimeProcess]:
    runtime = RuntimeProcess(dirs)
    runtime.wait_until_ready()
    yield runtime
    if runtime.process.poll() is None:
        runtime.stop(hard=True)


def _cli(
    directories: Directories,
    *args: str,
    timeout: str = "10",
    python: str | None = None,
    repo: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """The coordinator CLI as its own process: ``runtime status`` for this repository."""
    command = [sys.executable, str(ROOT / "harness/orchestration/coordinator.py")]
    if python:
        command = [sys.executable, "-c", python]
    return subprocess.run(
        [
            *command,
            "runtime",
            "status",
            "--repo",
            str(repo or directories.repo),
            "--timeout",
            timeout,
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "XDG_RUNTIME_DIR": str(directories.xdg),
            "PYTHONPATH": str(ROOT),
        },
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _pipe_exists(address: Path, api: runtime_client._PipeApi) -> bool:
    """Whether the named pipe exists. `Path.exists` would connect to the pipe."""
    try:
        api.WaitNamedPipe(str(address), 1)
    except OSError as exc:
        return bool(getattr(exc, "winerror", None) != 2)
    return True


if sys.platform == "win32":

    def _endpoint_exists(address: Path) -> bool:
        return _pipe_exists(address, runtime_client._windows_pipe_api())

else:

    def _endpoint_exists(address: Path) -> bool:
        """Whether the socket file exists."""
        return address.exists()


def test_the_coordinator_cli_reads_the_state_of_a_real_runtime(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    result = _cli(dirs)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["protocol"] == {"major": 1, "minor": 5}
    assert report["features"] == ["status"]
    assert report["store_id"] is None
    assert report["runtime"]["state"] == "serving"
    # Both languages derive the same repo id and the same socket address.
    assert report["runtime"]["repo_id"] == real_runtime.paths.repo_id
    assert report["socket"] == str(real_runtime.paths.socket)


def test_a_linked_worktree_reaches_the_runtime_of_its_main_checkout(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    admin = dirs.repo / ".git/worktrees/w"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    worktree = dirs.base / "w"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n")

    result = _cli(dirs, repo=worktree)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runtime"]["repo_id"] == real_runtime.paths.repo_id


def test_runtime_status_takes_no_state_dir(dirs: Directories) -> None:
    result = _cli(dirs, "--state-dir", str(dirs.base))

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


@UNIX_ONLY
def test_the_runtime_directory_the_socket_and_the_token_are_private(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    paths = real_runtime.paths

    assert _mode(paths.directory) == 0o700
    assert _mode(paths.socket) == 0o600
    assert _mode(paths.token) == 0o600
    assert len(paths.token.read_text()) == 64


def test_a_wrong_token_exits_2_as_unauthorized_peer(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    real_runtime.paths.token.write_text("0" * 64)

    result = _cli(dirs)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines()[1].startswith("REMEDY: [unauthorized_peer] ")


def test_an_incompatible_major_exits_2_as_protocol_unsupported(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    result = _cli(
        dirs,
        python=(
            "import sys\n"
            "from harness.orchestration import coordinator, runtime_client\n"
            "runtime_client.PROTOCOL_VERSION = (2, 0)\n"
            "sys.argv = ['coordinator', *sys.argv[1:]]\n"
            "raise SystemExit(coordinator.main())\n"
        ),
    )

    assert result.returncode == 2
    lines = result.stderr.splitlines()
    assert lines[0].startswith("ERROR: ")
    assert lines[1].startswith("REMEDY: [protocol_unsupported] update harness")


def _open_raw(address: Path) -> Connection:
    """A client connection of this platform to the runtime, with a limit of 10 seconds."""
    if sys.platform == "win32":
        return runtime_client._PipeConnection.open(str(address), lambda: 10.0)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    connection.connect(str(address))
    return connection


def _raw_session(runtime: RuntimeProcess) -> Connection:
    """A connection that has said hello with the real token."""
    connection = _open_raw(runtime.paths.socket)
    version = {"major": 1, "minor": 0}
    _write_frame(
        connection,
        {
            "protocol_version": version,
            "operation_id": "raw-hello",
            "operation": "hello",
            "request_deadline": int(time.time() * 1000) + 10_000,
            "payload": {
                "protocol": {"min": version, "max": version},
                "client_version": "raw",
                "repo_root": str(runtime.directories.repo),
                "token": runtime.paths.token.read_text(),
            },
        },
    )
    assert _read_frame(connection)["status"] == "ok"
    return connection


def _raw_request(operation: str, **extra: object) -> JsonObject:
    return {
        "protocol_version": {"major": 1, "minor": 0},
        "operation_id": f"raw-{operation}",
        "operation": operation,
        "request_deadline": int(time.time() * 1000) + 10_000,
        **extra,
    }


def test_an_unknown_request_field_is_malformed_request(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    with closing(_raw_session(real_runtime)) as connection:
        _write_frame(connection, _raw_request("status", surprise=1))
        refused = _read_frame(connection)
        _write_frame(connection, _raw_request("status"))
        served = _read_frame(connection)

    assert refused["status"] == "error"
    assert refused["error"]["code"] == "malformed_request"
    assert refused["operation_id"] == "raw-status"
    assert served["status"] == "ok"


def test_an_unknown_operation_is_unknown_operation(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call(
            "launch", operation_id="seam-1"
        )

    assert caught.value.code == "unknown_operation"
    assert caught.value.domain == "tooling"


@UNIX_ONLY
def test_without_xdg_the_runtime_and_the_client_meet_outside_the_repository(
    dirs: Directories,
) -> None:
    """The real fallback ``/tmp/harness-<uid>``: the test removes only its own lock file."""
    environ = {
        key: value for key, value in os.environ.items() if key != "XDG_RUNTIME_DIR"
    }
    paths = runtime_client.runtime_paths(dirs.repo, {})
    process = subprocess.Popen(
        [str(_runtime_binary()), "serve", "--repo", str(dirs.repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environ,
    )
    try:
        stdout = process.stdout
        assert stdout is not None
        ready = json.loads(stdout.readline() or "{}")
        reply = RuntimeClient(dirs.repo, environ={}).call(
            "status", operation_id="fallback"
        )
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
        (paths.directory / f"{paths.repo_id}.lock").unlink(missing_ok=True)

    assert ready.get("socket") == str(paths.socket), ready
    assert paths.directory == Path(f"/tmp/harness-{os.getuid()}")
    assert stat.S_IMODE(paths.directory.lstat().st_mode) == 0o700
    assert reply.result["repo_id"] == paths.repo_id
    assert not paths.socket.exists() and not paths.token.exists()
    assert not (dirs.repo / ".harness").exists()


def test_a_stopped_runtime_is_runtime_unavailable_and_leaves_no_socket(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    assert real_runtime.stop() == 0
    assert not _endpoint_exists(real_runtime.paths.socket)
    assert not real_runtime.paths.token.exists()

    result = _cli(dirs, timeout="0.3")

    assert result.returncode == 2
    assert result.stderr.splitlines()[1].startswith("REMEDY: [runtime_unavailable] ")
    assert not (dirs.repo / ".harness").exists()


def test_a_killed_runtime_is_replaced_and_its_stale_socket_removed(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    real_runtime.stop(hard=True)
    if sys.platform != "win32":
        # A killed Unix runtime leaves its socket file; a pipe goes away with its process.
        assert real_runtime.paths.socket.exists()
    stale_token = real_runtime.paths.token.read_text()

    replacement = RuntimeProcess(dirs)
    try:
        replacement.wait_until_ready()
        result = _cli(dirs)
        new_token = replacement.paths.token.read_text()
    finally:
        replacement.stop()

    assert result.returncode == 0, result.stderr
    assert new_token != stale_token


def test_a_request_repeats_until_the_runtime_starts(dirs: Directories) -> None:
    waiting = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "runtime",
            "status",
            "--repo",
            str(dirs.repo),
            "--timeout",
            "30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "XDG_RUNTIME_DIR": str(dirs.xdg)},
    )
    time.sleep(1.0)
    assert waiting.poll() is None
    runtime = RuntimeProcess(dirs)
    try:
        runtime.wait_until_ready()
        out, err = waiting.communicate(timeout=30)
    finally:
        runtime.stop()

    assert waiting.returncode == 0, err
    assert json.loads(out)["runtime"]["state"] == "serving"


def test_a_second_runtime_for_the_same_repository_is_refused_with_code_2(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    rival = RuntimeProcess(dirs)

    assert rival.process.wait(timeout=20) == 2
    assert rival.process.stderr is not None
    assert "already serves" in rival.process.stderr.read()
    # The refused rival must not have taken the first runtime's files.
    assert _cli(dirs).returncode == 0


def test_a_runtime_outside_a_git_working_tree_is_refused_with_code_2(
    dirs: Directories,
) -> None:
    refused = RuntimeProcess(dirs, repo=dirs.base)

    assert refused.process.wait(timeout=20) == 2
    assert refused.process.stderr is not None
    assert refused.process.stderr.read().startswith("ERROR: ")
