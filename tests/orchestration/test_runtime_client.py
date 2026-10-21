"""The Python client of ``harness-runtime`` and the ``runtime status`` command (ADR 0021).

Here a scripted runtime stands at the Unix socket, so each rule of the client is observed at the
connection: the address, the token, the retry of one ``operation_id`` and the typed errors with
exit code 2. The seam with the real Rust process, driven through the coordinator CLI, is at the end
of the module; CI builds the binary first (``HARNESS_REQUIRE_RUNTIME=1`` makes a missing one fail).
"""

from __future__ import annotations

import json
import os
import select
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
from pathlib import Path

import pytest

from harness.orchestration import coordinator, runtime_client
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the runtime IPC is a Unix domain socket; Windows uses a named pipe in a later ticket",
)

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 64
HEADER = struct.Struct(">I")
# A socket path is limited to 100 bytes, which a pytest or CI temporary directory can exceed. A
# writable /tmp is short; without one the temporary directory of the environment (TMPDIR) is used.
SHORT_TMP = "/tmp" if os.access("/tmp", os.W_OK | os.X_OK) else tempfile.gettempdir()


class Directories:
    """A repository and a runtime directory, both under a path short enough for a socket."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="hr", dir=SHORT_TMP)).resolve()
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
            threading.Thread(
                target=self._serve, args=(connection,), daemon=True
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
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
    assert runtime_client.repo_id(Path("/r/.git")) == "2d09593ea5e01029"


def test_the_socket_and_the_token_sit_in_the_xdg_runtime_directory(
    dirs: Directories,
) -> None:
    paths = runtime_client.runtime_paths(dirs.repo, dirs.environ)

    identity = runtime_client.repo_id(dirs.repo / ".git")
    assert paths.directory == dirs.xdg / "harness"
    assert paths.socket == dirs.xdg / "harness" / f"{identity}.sock"
    assert paths.token == dirs.xdg / "harness" / f"{identity}.token"


def test_without_xdg_the_runtime_directory_is_in_the_state_root(
    dirs: Directories,
) -> None:
    for environ in ({}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "relative/run"}):
        paths = runtime_client.runtime_paths(dirs.repo, environ)

        assert paths.directory == dirs.repo / ".harness/orchestration/state/runtime"


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


# -- seam 1: the coordinator CLI against a real ``harness-runtime serve`` -------------------------


def _runtime_binary() -> Path:
    configured = os.environ.get("HARNESS_RUNTIME_BIN")
    binary = (
        Path(configured)
        if configured
        else ROOT / "native/harness-runtime/target/debug/harness-runtime"
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
        )
        self.paths = runtime_client.runtime_paths(directories.repo, directories.environ)

    def wait_until_ready(self) -> dict[str, str]:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], 20)
        assert ready, "harness-runtime printed no readiness line within 20 seconds"
        line: dict[str, str] = json.loads(self.process.stdout.readline())
        assert line["status"] == "ready"
        return line

    def stop(self, sig: int = signal.SIGTERM) -> int:
        self.process.send_signal(sig)
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
        runtime.stop(signal.SIGKILL)


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


def test_the_coordinator_cli_reads_the_state_of_a_real_runtime(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    result = _cli(dirs)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["protocol"] == {"major": 1, "minor": 0}
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


def _raw_session(runtime: RuntimeProcess) -> socket.socket:
    """A connection that has said hello with the real token."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    connection.connect(str(runtime.paths.socket))
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
    with _raw_session(real_runtime) as connection:
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


def test_a_stopped_runtime_is_runtime_unavailable_and_leaves_no_socket(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    assert real_runtime.stop() == 0
    assert not real_runtime.paths.socket.exists()
    assert not real_runtime.paths.token.exists()

    result = _cli(dirs, timeout="0.3")

    assert result.returncode == 2
    assert result.stderr.splitlines()[1].startswith("REMEDY: [runtime_unavailable] ")
    assert not (dirs.repo / ".harness").exists()


def test_a_killed_runtime_is_replaced_and_its_stale_socket_removed(
    dirs: Directories, real_runtime: RuntimeProcess
) -> None:
    real_runtime.stop(signal.SIGKILL)
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
