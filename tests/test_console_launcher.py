"""harness.console.launcher: relaunching `harness console` under a pinned, project-independent
`uv run --with textual==<pin>` environment. Stdlib-only - importing this module (and the CLI's
`console` subcommand) must never require `textual` to be installed."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence
from unittest import mock

from harness.console import launcher
from harness.console.pin import TEXTUAL_PIN

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BIN_HARNESS = _REPO_ROOT / "harness" / "bin" / "harness"


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
    assert argv[argv.index("--with") + 1] == f"textual=={TEXTUAL_PIN}"
    assert argv[-2:] == ["console", str(repo)]
    assert str(launcher.BIN_HARNESS_PATH) in argv


def test_build_relaunch_argv_forwards_extra_argv() -> None:
    repo = Path("/tmp/some-repo")
    argv = launcher.build_relaunch_argv("/usr/local/bin/uv", repo, ["--foo", "bar"])
    assert argv[-2:] == ["--foo", "bar"]


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


def test_run_console_relaunches_via_the_injected_runner_when_uv_is_found() -> None:
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

    with mock.patch.dict("os.environ"):
        os.environ.pop(launcher.RELAUNCH_ENV, None)
        with mock.patch.object(launcher, "find_uv", return_value="/usr/local/bin/uv"):
            exit_code = launcher.run_console(repo, runner=fake_runner)

    assert exit_code == 0
    assert recorded_argv[0] == "/usr/local/bin/uv"
    assert recorded_env[launcher.RELAUNCH_ENV] == "1"


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _kill_process_tree(proc: "subprocess.Popen[bytes]") -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _console_subprocess(repo: Path, *, path_env: str) -> "subprocess.CompletedProcess[str]":
    """Output goes to temp files, not pipes: if a real `uv run` TUI leaks in, its grandchild
    processes inherit the pipes and `subprocess.run` would wait for their EOF forever after the
    timeout. On timeout the whole process tree is killed and the test fails instead of hanging."""
    env = dict(os.environ)
    env["PATH"] = path_env
    argv = [sys.executable, str(_BIN_HARNESS), "console", str(repo)]
    with tempfile.TemporaryFile("w+") as stdout, tempfile.TemporaryFile("w+") as stderr:
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
        return subprocess.CompletedProcess(argv, returncode, stdout.read(), stderr.read())


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
    """DoD: 'or the textual install fails, the command prints the reason and the text
    `harness health` report' - a fake `uv` script first on PATH exits non-zero, simulating an
    offline/failed `uv run --with textual==<pin>`."""
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

    assert result.returncode == 7
    assert "не удалось запустить textual через uv" in result.stdout
    assert "fake uv: textual resolution failed" in result.stderr
    assert "Итого:" in result.stdout
