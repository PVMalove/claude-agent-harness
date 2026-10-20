"""Tests for the isolated runner session helper (harness/health/isolated_runner_session.py)."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from harness.health import isolated_runner_session


@pytest.fixture(autouse=True)
def _writable_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The helper's temporary data goes to TMPDIR, so a read-only host /tmp never matters."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def test_stop_terminates_running_process() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    isolated_runner_session._stop(proc)
    assert proc.poll() is not None


def test_stop_noop_on_already_terminated_process() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    isolated_runner_session._stop(proc)
    assert proc.poll() is not None


def test_start_and_execute() -> None:
    proc = isolated_runner_session._start(
        [sys.executable, "-c", "pass"],
        os.environ,
    )
    assert proc.wait(timeout=5) == 0

    code = isolated_runner_session._execute(
        [sys.executable, "-c", "import sys; sys.exit(42)"],
        os.environ,
    )
    assert code == 42


def test_run_success() -> None:
    if sys.platform == "win32":
        pytest.skip("prctl is POSIX/Linux only")
    database: dict[str, object] = {
        "command": [sys.executable, "-c", "import time; time.sleep(5)"],
        "ready_command": [sys.executable, "-c", "pass"],
    }
    available, code = isolated_runner_session.run(
        database, [sys.executable, "-c", "pass"]
    )
    assert available is True
    assert code == 0


def test_run_creates_database_directory_in_tmpdir(_writable_tmpdir: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("prctl is POSIX/Linux only")
    seen = _writable_tmpdir / "seen"
    database: dict[str, object] = {
        "command": [sys.executable, "-c", "import time; time.sleep(5)"],
        "ready_command": [sys.executable, "-c", "pass"],
    }
    record = (
        "import os,sys; open(sys.argv[1],'w').write(os.environ['HARNESS_TEST_DATABASE_DIR'])"
    )
    available, code = isolated_runner_session.run(
        database, [sys.executable, "-c", record, str(seen)]
    )
    assert (available, code) == (True, 0)
    directory = Path(seen.read_text())
    assert directory.parent.name.startswith("harness-db-")
    assert directory.parent.parent == _writable_tmpdir
    assert not directory.exists()


def test_run_server_exits_prematurely() -> None:
    if sys.platform == "win32":
        pytest.skip("prctl is POSIX/Linux only")
    database: dict[str, object] = {
        "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
        "ready_command": [sys.executable, "-c", "import time; time.sleep(0.1)"],
    }
    available, code = isolated_runner_session.run(
        database, [sys.executable, "-c", "pass"]
    )
    assert available is False
    assert code == 3


def test_run_ready_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("prctl is POSIX/Linux only")
    monkeypatch.setattr(isolated_runner_session, "READY_SECONDS", 0.1)
    database: dict[str, object] = {
        "command": [sys.executable, "-c", "import time; time.sleep(5)"],
        "ready_command": [sys.executable, "-c", "import sys; sys.exit(1)"],
    }
    available, code = isolated_runner_session.run(
        database, [sys.executable, "-c", "pass"]
    )
    assert available is False
    assert code == 3


def test_run_fails_when_prctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCDLL:
        def prctl(self, *args: object) -> int:
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: FakeCDLL())
    available, code = isolated_runner_session.run({}, ["pass"])
    assert available is False
    assert code == 3


def test_run_fails_on_os_error() -> None:
    if sys.platform == "win32":
        pytest.skip("prctl is POSIX/Linux only")
    database: dict[str, object] = {
        "command": ["/nonexistent/executable/cannot/exist"],
        "ready_command": ["pass"],
    }
    available, code = isolated_runner_session.run(
        database, ["pass"]
    )
    assert available is False
    assert code == 3


def test_main_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        isolated_runner_session,
        "run",
        lambda db, cmd: (True, 0),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["isolated_runner_session.py", "{}", "[]"],
    )
    exit_code = isolated_runner_session.main()
    assert exit_code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"database": "ready"}


def test_main_negative_signal_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        isolated_runner_session,
        "run",
        lambda db, cmd: (True, -int(signal.SIGTERM)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["isolated_runner_session.py", "{}", "[]"],
    )
    exit_code = isolated_runner_session.main()
    assert exit_code == 128 + int(signal.SIGTERM)
    out = capsys.readouterr().out
    assert json.loads(out) == {"database": "ready"}


def test_main_unavailable_on_exception(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["isolated_runner_session.py", "invalid json", "[]"],
    )
    with pytest.raises(json.JSONDecodeError):
        isolated_runner_session.main()
    out = capsys.readouterr().out
    assert json.loads(out) == {"database": "unavailable"}
