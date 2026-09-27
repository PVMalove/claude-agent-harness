"""`harness health` end to end: the CLI wires harness.health.registry/render/report_json together,
runs every check without early exit, and exits 1 only when a check's status is 'fail'."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.health import registry as health_registry
from harness.health.context import HealthContext
from harness.health.model import CheckResult

CLI = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness")
)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_cmd_health_exits_1_when_a_check_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=False))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "отсутствует .harness/harness.lock" in out
    assert "Итого:" in out


def test_cmd_health_runs_every_check_without_early_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)

    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    ids = {check["id"] for check in data["checks"]}
    # Every registered group ran, not just the first failing one.
    assert {"files.lock", "files.agents_md", "repo_map.tier"} <= ids


def test_cmd_health_json_matches_schema_version_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["repo"] == str(tmp_path.resolve())
    assert data["online"] is False
    assert set(data["summary"]) == {"ok", "warn", "fail", "skipped"}
    assert data["fixes_applied"] == []
    assert exit_code == (1 if data["summary"]["fail"] else 0)


def test_cmd_health_exits_0_when_nothing_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)

    def _all_ok(_context: HealthContext) -> CheckResult:
        return CheckResult(
            id="test.probe", group="test", status="warn", message="not a failure"
        )

    monkeypatch.setattr(health_registry, "REGISTRY", [("test.probe", _all_ok)])

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == {"ok": 0, "warn": 1, "fail": 0, "skipped": 0}
    assert exit_code == 0


@pytest.mark.parametrize("lock_text", ["{broken", "[]"])
def test_broken_lock_is_a_fail_result_not_a_crash(tmp_path: Path, lock_text: str) -> None:
    """A lock that is not a JSON object is reported as `files.lock: fail` (with a remedy) while
    every other check still runs, and `--json` stays valid JSON."""
    _init_repo(tmp_path)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "harness.lock").write_text(lock_text, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness"),
            "health",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    checks = {check["id"]: check for check in data["checks"]}
    assert checks["files.lock"]["status"] == "fail"
    assert "повреждён" in checks["files.lock"]["message"]
    assert checks["files.lock"]["fix"] is not None
    assert "repo_map.tier" in checks
