"""harness.console.launcher: relaunching `harness console` under a pinned, project-independent
`uv run --with textual==<pin>` environment. Stdlib-only - importing this module (and the CLI's
`console` subcommand) must never require `textual` to be installed."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence, TypedDict, cast
from unittest import mock

import pytest

from harness.console import launcher
from harness.console.discovery import ServerEndpoint, discovery_path
from harness.console.pin import TEXTUAL_PIN

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_HARNESS = _REPO_ROOT / "harness" / "bin" / "harness.py"


UvAt = Callable[[str | None], None]


@pytest.fixture
def uv_at(monkeypatch: pytest.MonkeyPatch) -> UvAt:
    """Выбирает, где `find_uv` находит `uv` (None — не находит), и снимает `RELAUNCH_ENV`."""
    monkeypatch.delenv(launcher.RELAUNCH_ENV, raising=False)

    def choose(path: str | None) -> None:
        monkeypatch.setattr(launcher, "find_uv", lambda: path)

    return choose


def _ready_server(_repo: Path, _uv: str) -> ServerEndpoint:
    return ServerEndpoint(port=1, token="00" * 32)


def test_find_uv_uses_shutil_which() -> None:
    with mock.patch("shutil.which", return_value=None) as which:
        assert launcher.find_uv() is None
        which.assert_called_once_with("uv")

    with mock.patch("shutil.which", return_value="/usr/local/bin/uv") as which:
        assert launcher.find_uv() == "/usr/local/bin/uv"
        which.assert_called_once_with("uv")


def test_build_relaunch_argv_pins_textual_and_never_touches_the_project() -> None:
    repo = Path("/tmp/some-repo")
    argv = launcher.build_relaunch_argv("/usr/local/bin/uv", repo)

    assert argv[0] == "/usr/local/bin/uv"
    assert argv[1] == "run"
    # --no-project: uv run must never install or lock the surrounding project's own dependencies.
    assert "--no-project" in argv
    # --python: reuse the interpreter that passed the Python >= 3.12 check, not uv's first find.
    assert argv[argv.index("--python") + 1] == sys.executable
    assert argv[argv.index("--with") + 1] == f"textual=={TEXTUAL_PIN}"
    assert argv[-2:] == ["console", str(repo)]
    assert str(launcher.BIN_HARNESS_PATH) in argv


def test_build_relaunch_argv_forwards_extra_argv() -> None:
    repo = Path("/tmp/some-repo")
    argv = launcher.build_relaunch_argv("/usr/local/bin/uv", repo, ["--foo", "bar"])
    assert argv[-2:] == ["--foo", "bar"]


def test_build_server_argv_pins_the_server_requirements_and_never_touches_the_project() -> (
    None
):
    repo = Path("/tmp/some-repo")
    argv = launcher.build_server_argv("/usr/local/bin/uv", repo)

    assert argv == [
        "/usr/local/bin/uv",
        "run",
        "--no-project",
        "--python",
        sys.executable,
        "--with",
        "h11==0.16.0",
        "--with",
        "websockets==17.3",
        "--with",
        "jinja2==3.1.6",
        "python",
        str(launcher.BIN_HARNESS_PATH),
        "console-server",
        str(repo),
    ]


def test_restart_console_reexecutes_current_python_for_selected_project() -> None:
    repo = Path("/tmp/selected project")
    with mock.patch("os.execv", side_effect=OSError("exec failed")) as execv:
        with pytest.raises(OSError, match="exec failed"):
            launcher.restart_console(repo)
    execv.assert_called_once_with(
        sys.executable,
        [sys.executable, str(launcher.BIN_HARNESS_PATH), "console", str(repo)],
    )


def test_run_console_already_relaunched_runs_the_injected_app_runner() -> None:
    """Proves the "already relaunched" branch never needs `find_uv`/`runner` and dispatches to
    `app_runner` - injected here instead of monkeypatching `sys.modules["harness.console.app"]`,
    which is never safe to do in a process that may also run real textual Pilot tests."""
    repo = Path("/tmp/some-repo")
    calls: list[Path] = []

    def fake_app_runner(passed_repo: Path) -> int:
        calls.append(passed_repo)
        return 0

    with mock.patch.dict("os.environ", {launcher.RELAUNCH_ENV: "1"}):
        with mock.patch.object(launcher, "find_uv") as find_uv:
            exit_code = launcher.run_console(repo, app_runner=fake_app_runner)
            find_uv.assert_not_called()

    assert exit_code == 0
    assert calls == [repo]


def test_run_console_relaunches_via_the_injected_runner_when_uv_is_found(
    uv_at: UvAt,
) -> None:
    repo = Path("/tmp/some-repo")
    recorded_argv: list[str] = []
    recorded_env: dict[str, str] = {}

    def fake_runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        recorded_argv.extend(argv)
        if env is not None:
            recorded_env.update(env)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        repo, runner=fake_runner, server_starter=_ready_server
    )

    assert exit_code == 0
    assert recorded_argv[0] == "/usr/local/bin/uv"
    assert recorded_env[launcher.RELAUNCH_ENV] == "1"


def test_a_tui_failure_after_start_reports_the_exit_code_without_the_fallback(
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    """The fallback is for a failed start (no uv, offline, textual install failure): once the
    relaunched process reached the TUI, a non-zero exit is the TUI's own and only its code is
    reported, never "не удалось запустить textual"."""
    repo = Path("/tmp/some-repo")

    def started_then_failed(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        assert env is not None
        Path(
            env[launcher.STARTED_ENV]
        ).touch()  # what the relaunched process does on start
        return subprocess.CompletedProcess(list(argv), 3, "", "")

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        repo, runner=started_then_failed, server_starter=_ready_server
    )

    out = capsys.readouterr().out
    assert exit_code == 3
    assert "завершился с кодом 3" in out
    assert "не удалось запустить" not in out
    assert "Итого:" not in out


def test_a_failed_tui_relaunch_prints_reason_and_health_and_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    def failed_before_start(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(list(argv), 5, "", "")

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        repo, runner=failed_before_start, server_starter=_ready_server
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "не удалось запустить textual через uv" in out
    assert "Итого:" in out


def test_the_relaunched_process_marks_that_the_tui_started(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    with mock.patch.dict(
        "os.environ", {launcher.RELAUNCH_ENV: "1", launcher.STARTED_ENV: str(marker)}
    ):
        assert launcher.run_console(tmp_path, app_runner=lambda _repo: 0) == 0
    assert marker.is_file()


def _no_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    raise AssertionError(f"the TUI relaunch must not run for --web: {argv}")


def test_web_prints_the_token_url_opens_the_browser_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    repo = Path("/tmp/some-repo")
    token = "ab" * 32
    started: list[tuple[Path, str]] = []
    opened: list[str] = []

    def fake_starter(passed_repo: Path, uv: str) -> ServerEndpoint:
        started.append((passed_repo, uv))
        return ServerEndpoint(port=43210, token=token)

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        repo,
        web=True,
        runner=_no_runner,
        server_starter=fake_starter,
        browser_opener=opened.append,
    )

    url = f"http://127.0.0.1:43210/?token={token}"
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [url]
    assert opened == [url]
    assert started == [(repo, "/usr/local/bin/uv")]


def test_web_no_open_prints_the_url_without_opening_the_browser(
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    opened: list[str] = []

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        Path("/tmp/some-repo"),
        web=True,
        no_open=True,
        runner=_no_runner,
        server_starter=lambda _repo, _uv: ServerEndpoint(7, "cd" * 32),
        browser_opener=opened.append,
    )

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "http://127.0.0.1:7/?token=" + "cd" * 32
    ]
    assert opened == []


def _failing_starter(repo: Path, uv: str) -> ServerEndpoint:
    raise launcher.ServerStartError("сервер завершился с кодом 7")


@pytest.mark.parametrize("web", [True, False], ids=["web", "tui"])
def test_a_server_start_failure_prints_reason_and_health_and_exits_one(
    web: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    """TUI без сервера не стартует: `_no_runner` падает, если дело дошло до перезапуска TUI."""
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    opened: list[str] = []

    uv_at("/usr/local/bin/uv")
    exit_code = launcher.run_console(
        repo,
        web=web,
        runner=_no_runner,
        server_starter=_failing_starter,
        browser_opener=opened.append,
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "не удалось запустить сервер консоли" in out
    assert "сервер завершился с кодом 7" in out
    assert "Итого:" in out
    assert opened == []


def test_web_without_uv_prints_reason_and_health_and_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    uv_at: UvAt,
) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    uv_at(None)
    exit_code = launcher.run_console(
        repo,
        web=True,
        runner=_no_runner,
        server_starter=_failing_starter,
        browser_opener=lambda _url: pytest.fail("browser must stay closed"),
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "uv не найден" in out
    assert "Итого:" in out


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _kill_process_tree(proc: "subprocess.Popen[bytes]") -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
        )
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _console_subprocess(
    repo: Path, *, path_env: str, flags: Sequence[str] = ()
) -> "subprocess.CompletedProcess[str]":
    """Output goes to temp files, not pipes: if a real `uv run` TUI leaks in, its grandchild
    processes inherit the pipes and `subprocess.run` would wait for their EOF forever after the
    timeout. On timeout the whole process tree is killed and the test fails instead of hanging."""
    env = dict(os.environ)
    env["PATH"] = path_env
    argv = [sys.executable, str(_BIN_HARNESS), "console", str(repo), *flags]
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8") as stdout,
        tempfile.TemporaryFile("w+", encoding="utf-8") as stderr,
    ):
        proc = subprocess.Popen(
            argv, stdout=stdout, stderr=stderr, env=env, start_new_session=True
        )
        try:
            returncode = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            raise
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            argv, returncode, stdout.read(), stderr.read()
        )


def test_console_prints_reason_and_health_report_when_uv_is_missing(
    tmp_path: Path,
) -> None:
    """DoD: 'When uv is missing ... the command prints the reason and the text `harness health`
    report' - proved end to end through a real subprocess, PATH scrubbed of uv entirely."""
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    # shutil.which per entry honours PATHEXT, so a Windows `uv.EXE` is scrubbed too - the same
    # lookup launcher.find_uv() does.
    scrubbed_path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and shutil.which("uv", path=entry) is None
    )

    result = _console_subprocess(repo, path_env=scrubbed_path)

    assert result.returncode == 1
    assert "uv не найден" in result.stdout
    # The same marker tests/test_health_cli.py asserts for a real `harness health` run.
    assert "Итого:" in result.stdout


def test_console_prints_reason_and_health_report_when_relaunch_fails(
    tmp_path: Path,
) -> None:
    """DoD: 'when the launch fails, the command prints the reason and the text `harness health`
    report, exit code 1' - a fake `uv` script first on PATH exits non-zero, simulating an
    offline/failed `uv run --with ...` that starts the console server."""
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\necho 'fake uv: textual resolution failed (offline)' >&2\nexit 7\n",
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # Windows: shutil.which only resolves PATHEXT names, so the extensionless sh script is
    # invisible there; this batch twin is found first instead.
    (fake_bin / "uv.cmd").write_text(
        "@echo off\necho fake uv: textual resolution failed (offline) 1>&2\nexit /b 7\n",
        encoding="utf-8",
    )

    path_env = os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")])
    result = _console_subprocess(repo, path_env=path_env)

    assert result.returncode == 1
    assert "не удалось запустить сервер консоли" in result.stdout
    assert "uv run завершился с кодом 7" in result.stdout
    assert "fake uv: textual resolution failed" in result.stdout
    assert "Итого:" in result.stdout


class _Record(TypedDict):
    port: int
    token: str
    pid: int


@contextlib.contextmanager
def _running_console_server(repo: Path) -> Iterator[tuple[int, _Record]]:
    """`harness console-server <repo>` as its own process: yields its pid and published record."""
    record_path = discovery_path(repo)
    argv = [sys.executable, str(_BIN_HARNESS), "console-server", str(repo)]

    with tempfile.TemporaryFile("w+", encoding="utf-8") as output:
        proc = subprocess.Popen(
            argv, stdout=output, stderr=output, start_new_session=True
        )
        try:
            deadline = time.monotonic() + 30
            while not record_path.exists() and proc.poll() is None:
                assert time.monotonic() < deadline, "discovery record never appeared"
                time.sleep(0.05)
            output.seek(0)
            assert proc.poll() is None, output.read()
            yield (
                proc.pid,
                cast(_Record, json.loads(record_path.read_text(encoding="utf-8"))),
            )
        finally:
            if proc.poll() is None:
                _kill_process_tree(proc)


def test_console_server_command_publishes_the_discovery_record(tmp_path: Path) -> None:
    """`harness console-server <repo>` is what the launcher starts under `uv run --with`: it must
    serve the project until idle and publish port, token and pid in the discovery record."""
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    with _running_console_server(repo) as (pid, record):
        pass

    assert record["pid"] == pid
    assert 0 < record["port"] < 65536
    assert re.fullmatch(r"[0-9a-f]{64}", record["token"])


def _fake_uv_that_runs_the_command(bin_dir: Path) -> None:
    """A `uv` stand-in: drops the `run --with ...` options and runs the command after `python` with
    the test interpreter, which already has the dev pins installed."""
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "rest = sys.argv[sys.argv.index('python') + 1:]\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *rest])\n",
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(sys.platform == "win32", reason="the uv stand-in is a POSIX script")
def test_web_starts_a_server_that_outlives_the_command(tmp_path: Path) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    _fake_uv_that_runs_the_command(tmp_path / "fake-bin")
    path_env = os.pathsep.join([str(tmp_path / "fake-bin"), os.environ.get("PATH", "")])
    record: dict[str, object] = {}

    try:
        result = _console_subprocess(
            repo, path_env=path_env, flags=["--web", "--no-open"]
        )
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads(discovery_path(repo).read_text(encoding="utf-8"))
        os.kill(
            int(str(record["pid"])), 0
        )  # the server is still alive after the command exited
    finally:
        if record:
            os.kill(int(str(record["pid"])), signal.SIGKILL)

    assert result.stdout.splitlines() == [
        f"http://127.0.0.1:{record['port']}/?token={record['token']}"
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="the uv stand-in is a POSIX script")
def test_web_reuses_a_running_server_without_starting_another(tmp_path: Path) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    failing_uv = fake_bin / "uv"
    failing_uv.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    failing_uv.chmod(
        failing_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    path_env = os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")])

    with _running_console_server(repo) as (_pid, record):
        result = _console_subprocess(
            repo, path_env=path_env, flags=["--web", "--no-open"]
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f"http://127.0.0.1:{record['port']}/?token={record['token']}"
    ]


def test_start_server_reports_a_uv_that_cannot_be_executed(tmp_path: Path) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)

    with pytest.raises(launcher.ServerStartError, match="не удалось запустить uv"):
        launcher.start_server(repo, str(tmp_path / "no-such-uv"))


@pytest.mark.skipif(sys.platform == "win32", reason="the uv stand-in is a POSIX script")
def test_start_server_gives_up_when_no_record_appears_in_time(tmp_path: Path) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    silent_uv = tmp_path / "uv"
    silent_uv.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    silent_uv.chmod(
        silent_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    with pytest.raises(launcher.ServerStartError, match="сервер не ответил за 0.5 с"):
        launcher.start_server(repo, str(silent_uv), timeout=0.5)


@pytest.mark.skipif(sys.platform == "win32", reason="the uv stand-in is a POSIX script")
def test_start_server_ignores_a_record_whose_port_belongs_to_another_program(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    _fake_uv_that_runs_the_command(tmp_path / "fake-bin")
    record_path = discovery_path(repo)
    record_path.parent.mkdir(parents=True, mode=0o700)
    pid = 0

    # A foreign program holds the port: it accepts connections (kernel backlog) and never answers.
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        foreign_port = foreign.getsockname()[1]
        record_path.write_text(
            json.dumps({"port": foreign_port, "token": "ee" * 32, "pid": 1}),
            encoding="utf-8",
        )
        try:
            endpoint = launcher.start_server(repo, str(tmp_path / "fake-bin" / "uv"))
            pid = json.loads(record_path.read_text(encoding="utf-8"))["pid"]
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)

    assert endpoint.port != foreign_port
    assert endpoint.token != "ee" * 32
