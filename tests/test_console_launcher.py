"""harness.console.launcher: relaunching `harness console` under a pinned, project-independent
`uv run --with textual==<pin>` environment. Stdlib-only - importing this module (and the CLI's
`console` subcommand) must never require `textual` to be installed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Sequence
from unittest import mock

from harness.console import launcher
from harness.console.pin import TEXTUAL_PIN


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


def test_run_console_already_relaunched_runs_the_app_in_process() -> None:
    repo = Path("/tmp/some-repo")
    calls: list[Path] = []

    class _FakeAppModule:
        @staticmethod
        def run(passed_repo: Path) -> int:
            calls.append(passed_repo)
            return 0

    with mock.patch.dict("os.environ", {launcher.RELAUNCH_ENV: "1"}):
        with mock.patch.object(launcher, "find_uv") as find_uv:
            with mock.patch.dict(
                "sys.modules", {"harness.console.app": _FakeAppModule}
            ):
                exit_code = launcher.run_console(repo)
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

    import os as _os

    _os.environ.pop(launcher.RELAUNCH_ENV, None)
    with mock.patch.object(launcher, "find_uv", return_value="/usr/local/bin/uv"):
        exit_code = launcher.run_console(repo, runner=fake_runner)

    assert exit_code == 0
    assert recorded_argv[0] == "/usr/local/bin/uv"
    assert recorded_env[launcher.RELAUNCH_ENV] == "1"
