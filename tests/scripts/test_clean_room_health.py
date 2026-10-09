"""Health-вывод clean-room: краткий терминал, долговечные логи и исходные ошибки CLI."""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

support = importlib.import_module("scripts.clean_room.support")


def _health_cli(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    report: dict[str, object],
    *,
    exit_code: int = 0,
) -> None:
    script = (
        "import sys; print(" + repr(json.dumps(report)) + f"); sys.exit({exit_code})"
    )
    monkeypatch.setattr(support, "ROOT", root)
    monkeypatch.setattr(support, "HARNESS", [sys.executable, "-c", script])


def _report(status: str, message: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "summary": {
            "ok": 0,
            "warn": int(status == "warn"),
            "fail": int(status == "fail"),
            "skipped": 0,
        },
        "checks": [
            {"id": "test.check", "status": status, "message": message, "fix": None}
        ],
    }


def test_success_keeps_unique_full_logs_after_project_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "runs" / "project"
    project.mkdir(parents=True)
    _health_cli(monkeypatch, tmp_path, _report("warn", "full warning details"))

    support.run_health(project)
    support.run_health(project)
    shutil.rmtree(project.parent)

    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2
    assert "ok=0 warn=1 fail=0 skipped=0" in output
    assert "full warning details" not in output
    logs = list((tmp_path / ".harness" / ".sandboxes" / "logs").glob("*.log"))
    assert len(logs) == 2
    for log in logs:
        assert str(log) in output
        assert "full warning details" in log.read_text(encoding="utf-8")


def test_failed_check_shows_reason_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _health_cli(
        monkeypatch,
        tmp_path,
        _report("fail", "missing config; token=private-value"),
        exit_code=1,
    )

    with pytest.raises(SystemExit) as error:
        support.run_health(tmp_path / "project")

    assert error.value.code == 1
    output = capsys.readouterr()
    assert "fail=1" in output.out
    assert "missing config" in output.err
    logs = list((tmp_path / ".harness" / ".sandboxes" / "logs").glob("*.log"))
    assert len(logs) == 1
    assert str(logs[0]) in output.out
    assert "private-value" not in output.out + output.err + logs[0].read_text(
        encoding="utf-8"
    )


def test_cli_error_preserves_exit_code_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(support, "ROOT", tmp_path)
    monkeypatch.setattr(
        support,
        "HARNESS",
        [
            sys.executable,
            "-c",
            "import sys; print('context ' * 1000 + '\\nCLI unavailable', file=sys.stderr); sys.exit(7)",
        ],
    )

    with pytest.raises(SystemExit) as error:
        support.run_health(tmp_path / "project")

    assert error.value.code == 7
    output = capsys.readouterr()
    assert "CLI unavailable" in output.err
    assert len(output.err) < 1000
    logs = list((tmp_path / ".harness" / ".sandboxes" / "logs").glob("*.log"))
    assert len(logs) == 1
    assert "CLI unavailable" in logs[0].read_text(encoding="utf-8")
    assert "context " * 1000 in logs[0].read_text(encoding="utf-8")


def test_invalid_report_cannot_pass_with_zero_cli_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(support, "ROOT", tmp_path)
    monkeypatch.setattr(support, "HARNESS", [sys.executable, "-c", "print('not JSON')"])

    with pytest.raises(SystemExit) as error:
        support.run_health(tmp_path / "project")

    assert error.value.code == 1
