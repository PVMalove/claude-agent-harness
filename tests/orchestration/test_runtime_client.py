"""The Python client of ``harness-runtime`` and the ``runtime status`` command (ADR 0021).

Here a scripted runtime stands at the Unix socket, so each rule of the client is observed at the
connection: the address, the token, the retry of one ``operation_id`` and the typed errors with
exit code 2. The seam with the real Rust process is at the end of the module.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness.orchestration import coordinator, runtime_client
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 64
HEADER = struct.Struct(">I")


class Directories:
    """A repository and a runtime directory, both under a path short enough for a socket."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="hr")).resolve()
        self.repo = self.base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.xdg = self.base / "run"
        self.xdg.mkdir()

    @property
    def environ(self) -> dict[str, str]:
        return {"XDG_RUNTIME_DIR": str(self.xdg)}


@pytest.fixture
def dirs() -> Iterator[Directories]:
    directories = Directories()
    yield directories
    shutil.rmtree(directories.base, ignore_errors=True)


def _read(connection: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _read_frame(connection: socket.socket) -> JsonObject:
    (length,) = HEADER.unpack(_read(connection, HEADER.size))
    loaded: JsonObject = json.loads(_read(connection, length))
    return loaded


def _write_frame(connection: socket.socket, message: JsonObject) -> None:
    body = json.dumps(message).encode()
    connection.sendall(HEADER.pack(len(body)) + body)


def _ok(operation_id: object, result: JsonObject) -> JsonObject:
    return {"operation_id": operation_id, "status": "ok", "revision": None, "result": result}


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
    return {"operation_id": operation_id, "status": "error", "revision": None, "error": error}


HELLO_RESULT: JsonObject = {
    "protocol": {"major": 1, "minor": 0},
    "server_version": "0.1.0",
    "store_id": None,
    "features": ["status"],
}


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
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.paths.socket))
        self.listener.listen()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.listener.close()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            if self.connections <= self.drop_connections:
                connection.close()
                continue
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            try:
                while True:
                    request = _read_frame(connection)
                    self.requests.append(request)
                    if request["operation"] == "hello":
                        payload = request["payload"]
                        if payload["token"] != TOKEN:
                            _write_frame(connection, _error(request["operation_id"], "unauthorized_peer"))
                            return
                        if self.answer is not None and (
                            refusal := self.answer(request)
                        ).get("status") == "error":
                            _write_frame(connection, refusal)
                            return
                        _write_frame(connection, _ok(request["operation_id"], HELLO_RESULT))
                    elif self.answer is not None:
                        _write_frame(connection, self.answer(request))
                    else:
                        _write_frame(connection, _ok(request["operation_id"], {"state": "serving"}))
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
    assert runtime_client.repo_id(Path("/r/.git")) == "2d09593ea5e01029"


def test_the_socket_and_the_token_sit_in_the_xdg_runtime_directory(dirs: Directories) -> None:
    paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)

    identity = runtime_client.repo_id(dirs.repo / ".git")
    assert paths.directory == dirs.xdg / "harness"
    assert paths.socket == dirs.xdg / "harness" / f"{identity}.sock"
    assert paths.token == dirs.xdg / "harness" / f"{identity}.token"


def test_without_xdg_the_runtime_directory_is_in_the_state_root(dirs: Directories) -> None:
    for environ in ({}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "relative/run"}):
        paths = runtime_client.runtime_paths(dirs.repo, environ)

        assert paths.directory == dirs.repo / ".harness/orchestration/state/runtime"


def test_a_linked_worktree_shares_the_runtime_of_its_main_checkout(dirs: Directories) -> None:
    admin = dirs.repo / ".git/worktrees/w"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    worktree = dirs.base / "w"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n")

    assert runtime_client.runtime_paths(worktree, dirs.environ) == runtime_client.runtime_paths(
        dirs.repo, dirs.environ
    )


def test_a_directory_that_is_no_repository_has_no_address(dirs: Directories) -> None:
    with pytest.raises(RuntimeClientError) as caught:
        runtime_client.runtime_paths(dirs.base, dirs.environ)

    assert caught.value.code == "runtime_unavailable"


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

    reply = RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="op-1")

    assert reply.result == {"state": "serving"}
    assert reply.hello["server_version"] == "0.1.0"
    (hello,) = runtime.operations("hello")
    assert hello["payload"]["token"] == TOKEN
    assert hello["payload"]["protocol"] == {
        "min": {"major": 1, "minor": 0},
        "max": {"major": 1, "minor": 0},
    }
    assert hello["payload"]["repo_root"] == str(dirs.repo)
    (status,) = runtime.operations("status")
    assert status["operation_id"] == "op-1"
    assert status["protocol_version"] == {"major": 1, "minor": 0}


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
    assert [request["operation_id"] for request in runtime.operations("status")] == ["same-intent"]
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
        ScriptedRuntime(dirs, answer=lambda request: _error(request["operation_id"], "from_the_future"))
    )

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="u")

    assert caught.value.code == "internal"
    assert "from_the_future" in caught.value.message


def test_a_field_the_client_does_not_know_is_ignored(
    dirs: Directories, runtimes: list[ScriptedRuntime]
) -> None:
    def answer(request: JsonObject) -> JsonObject:
        return {**_ok(request["operation_id"], {"state": "serving"}), "added_later": [1]}

    runtimes.append(ScriptedRuntime(dirs, answer=answer))

    reply = RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="f")

    assert reply.result == {"state": "serving"}


def test_a_wrong_token_is_unauthorized_peer(dirs: Directories, runtimes: list[ScriptedRuntime]) -> None:
    runtime = ScriptedRuntime(dirs)
    runtimes.append(runtime)
    runtime.paths.token.write_text("someone-elses-token")

    with pytest.raises(RuntimeClientError) as caught:
        RuntimeClient(dirs.repo, environ=dirs.environ).call("status", operation_id="w")

    assert caught.value.code == "unauthorized_peer"


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
        "del socket.AF_UNIX\n"
        "del os.getuid\n"
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
        sys, "argv", ["coordinator", "runtime", "status", "--repo", str(directories.repo), *extra]
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
    assert report["socket"].endswith(".sock")


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
        ScriptedRuntime(dirs, answer=lambda request: _error(request["operation_id"], "protocol_unsupported"))
    )

    code, _, err = _run_command(monkeypatch, capsys, dirs)

    assert code == 2
    assert err.splitlines()[1] == "REMEDY: [protocol_unsupported] fix protocol_unsupported"
