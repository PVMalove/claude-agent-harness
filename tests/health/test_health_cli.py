"""Сквозные тесты CLI команды harness health."""

from __future__ import annotations

import json
import os
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
    str(Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py")
)


def _init_repo(path: Path) -> None:
    """Инициализировать пустой git-репозиторий по указанному пути."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_cmd_health_exits_1_when_a_check_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что CLI возвращает код 1 при наличии упавшей проверки."""
    _init_repo(tmp_path)

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=False))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "отсутствует .harness/harness.lock" in out
    assert "Итого:" in out


def test_cmd_health_runs_every_check_without_early_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что CLI запускает все проверки без преждевременного выхода."""
    _init_repo(tmp_path)

    CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    ids = {check["id"] for check in data["checks"]}
    # Every registered group ran, not just the first failing one.
    assert {"files.lock", "files.agents_md", "repo_map.tier"} <= ids


def test_cmd_health_json_matches_schema_version_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Проверить, что вывод health --json соответствует версии схемы 1."""
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
    """Проверить, что CLI возвращает код 0, если нет упавших проверок."""
    _init_repo(tmp_path)

    def _all_ok(_context: HealthContext) -> CheckResult:
        """Вернуть результат проверки со статусом warn."""
        return CheckResult(
            id="test.probe", group="test", status="warn", message="not a failure"
        )

    monkeypatch.setattr(health_registry, "REGISTRY", [("test.probe", _all_ok)])

    exit_code = CLI["cmd_health"](SimpleNamespace(repo=str(tmp_path), json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == {"ok": 0, "warn": 1, "fail": 0, "skipped": 0}
    assert exit_code == 0


@pytest.mark.parametrize(
    "lock_text",
    ["{broken", "[]", "[" * 100_000],
    ids=["invalid", "not-object", "too-deep"],
)
def test_broken_lock_is_a_fail_result_not_a_crash(
    tmp_path: Path, lock_text: str
) -> None:
    """Проверить, что поврежденный файл lock приводит к статусу fail, а не к падению процесса."""
    _init_repo(tmp_path)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "harness.lock").write_text(lock_text, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"),
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
    # Lock-dependent checks point at the broken lock instead of claiming the lock is missing or
    # that backend-orchestration was never selected.
    for check_id in (
        "files.skill_registry",
        "files.orchestration_config",
        "orchestration.ledger_summary",
    ):
        assert checks[check_id]["status"] == "skipped"
        assert "повреждён" in checks[check_id]["message"]
    assert not any("не выбрана" in check["message"] for check in data["checks"])


@pytest.mark.parametrize("pythonpath_first", [True, False])
def test_cli_imports_the_harness_package_not_itself(
    tmp_path: Path, pythonpath_first: bool
) -> None:
    """Проверить, что CLI импортирует пакет harness, а не скрипт harness.py."""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if pythonpath_first:
        env["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [sys.executable, str(root / "harness" / "bin" / "harness.py"), "--help"],
        cwd=root / "harness" / "bin",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "health" in result.stdout
