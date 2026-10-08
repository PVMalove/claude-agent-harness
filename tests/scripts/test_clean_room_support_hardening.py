"""Шаги clean-room ограничены по времени: зависший шаг завершает прогон с именем команды."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

support = importlib.import_module("scripts.clean_room.support")


def test_a_hung_step_exits_with_its_command(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(30)"]

    with pytest.raises(SystemExit) as raised:
        support.run_step(command, cwd=tmp_path, timeout=0.5)

    message = str(raised.value.code)
    assert message.startswith("clean-room step timed out after 0.5s: ")
    assert "time.sleep(30)" in message


def test_a_step_keeps_its_arguments_and_gets_the_default_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    support.run_step(["git", "status"], cwd=tmp_path, check=True, input="x")

    assert seen == {
        "cwd": tmp_path,
        "check": True,
        "input": "x",
        "timeout": support.STEP_TIMEOUT_SECONDS,
    }


@pytest.mark.parametrize(
    ("helper", "returncode", "stdout"),
    [
        ("run_ok", 0, ""),
        ("run_fails", 1, ""),
        ("fail_output", 1, ""),
        ("capture", 0, "out"),
        ("fail_json", 1, "{}"),
        ("capture_json", 0, "{}"),
    ],
)
def test_every_command_helper_is_bounded(
    monkeypatch: pytest.MonkeyPatch, helper: str, returncode: int, stdout: str
) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    getattr(support, helper)(["harness", "health"])

    assert seen["timeout"] == support.STEP_TIMEOUT_SECONDS


def test_a_hook_keeps_its_own_shorter_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    support.run_hook(tmp_path / "hook.sh", tmp_path, "echo ok")

    assert seen["timeout"] == 10
