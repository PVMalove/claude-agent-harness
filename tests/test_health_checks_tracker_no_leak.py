"""Regression shield (ticket #346): a tracker check's CheckResult.message must never contain the
raw stdout/stderr of an auth-adjacent CLI call, since gh/glab auth output can carry token-adjacent
detail. Only the parsed, specific fields health actually needs (a return code, or named JSON
fields such as .permissions/.name/.color) may cross into a CheckResult; the full response body
never does, even when a call fails."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.health.checks import tracker
from harness.health.context import HealthContext

_SENSITIVE_MARKER = "gho_supersecrettoken1234567890"


def _online_context(tmp_path: Path) -> HealthContext:
    return HealthContext(repo=tmp_path, lock=None, online=True)


def _fake_result(returncode: int, stdout: str, stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def test_failed_auth_status_never_echoes_its_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        tracker,
        "_run",
        lambda *_args, **_kwargs: _fake_result(
            1, "", f"authentication failed, token {_SENSITIVE_MARKER}"
        ),
    )

    result = tracker.check_auth(_online_context(tmp_path))

    assert result.status == "fail"
    assert _SENSITIVE_MARKER not in result.message


def test_successful_auth_status_never_echoes_its_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tracker,
        "_run",
        lambda *_args, **_kwargs: _fake_result(
            0, f"Logged in as octocat with token {_SENSITIVE_MARKER}", ""
        ),
    )

    result = tracker.check_auth(_online_context(tmp_path))

    assert result.status == "ok"
    assert _SENSITIVE_MARKER not in result.message


def test_permissions_failure_never_echoes_the_raw_api_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tracker,
        "_run",
        lambda *_args, **_kwargs: _fake_result(
            1, "", f"HTTP 401: bad credentials ({_SENSITIVE_MARKER})"
        ),
    )

    result = tracker.check_permissions(_online_context(tmp_path))

    assert result.status == "warn"
    assert _SENSITIVE_MARKER not in result.message
