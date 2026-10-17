"""Тесты заполнения полей test_paths и test_runner в .harness/project.json при установке харнесса."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from harness.health.project_files import validate_project_json

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")
# Every other project.json value, so that init asks nothing.
PROJECT_FLAGS = [
    "--language",
    "ru",
    "--pr-base-branch",
    "main",
    "--branch-pattern",
    "^feature/.+",
    "--qa-gate-command",
    "echo test",
    "--tracker-type",
    "local",
]


def _repo(path: Path) -> Path:
    """Создать пустой git-репозиторий."""
    path.mkdir(parents=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    return path


def _init(repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Установить pvmalove-suite без интерактивного ввода."""
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(repo),
            "--capability",
            "pvmalove-suite",
            *PROJECT_FLAGS,
            *flags,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _installed_project_json(repo: Path) -> dict[str, object]:
    """Прочитать установленный project.json и убедиться, что он проходит строгий контракт."""
    problems: list[str] = []
    validate_project_json(repo, problems)
    assert problems == []
    data = json.loads((repo / ".harness" / "project.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_install_without_test_flags_leaves_the_fields_out(tmp_path: Path) -> None:
    """Проверить, что без флагов поля не пишутся и тест-автор остаётся выключенным."""
    repo = _repo(tmp_path / "repo")

    result = _init(repo)

    assert result.returncode == 0, result.stderr
    data = _installed_project_json(repo)
    assert "test_paths" not in data
    assert "test_runner" not in data


def test_install_writes_the_test_fields_given_by_flags(tmp_path: Path) -> None:
    """Проверить, что флаги пишут test_paths по порядку без повторов и argv test_runner без шелла."""
    repo = _repo(tmp_path / "repo")

    result = _init(
        repo,
        "--test-path",
        "tests/**",
        "--test-path",
        "spec/**",
        "--test-path",
        "tests/**",
        "--test-runner-command",
        "python3 -m unittest '{test_id}'",
    )

    assert result.returncode == 0, result.stderr
    data = _installed_project_json(repo)
    assert data["test_paths"] == ["tests/**", "spec/**"]
    assert data["test_runner"] == {
        "command": ["python3", "-m", "unittest", "{test_id}"]
    }


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--test-path", "../secret-zone/**"),
        ("--test-path", "/abs/secret-zone/**"),
        ("--test-runner-command", "python3 -m unittest secret-case"),
        ("--test-runner-command", "{test_id} secret-case"),
        ("--test-runner-command", "python3 'secret-case"),
    ],
)
def test_install_rejects_an_invalid_test_flag_before_writing(
    tmp_path: Path, flag: str, value: str
) -> None:
    """Проверить, что некорректный флаг теста отклоняется до записи файлов и не печатается."""
    repo = _repo(tmp_path / "repo")

    result = _init(repo, flag, value)

    assert result.returncode == 2
    assert flag in result.stderr
    assert "secret" not in result.stderr
    assert not (repo / ".harness").exists()


def test_install_optional_build_database_and_health(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    result = _init(
        repo,
        "--test-path",
        "tests/**",
        "--test-runner-command",
        "pytest {test_id}",
        "--test-build-command",
        "cc src/main.c -o artifacts/main",
        "--test-artifact-path",
        "artifacts/**",
        "--test-database-engine",
        "postgresql",
        "--test-database-test-id",
        "tests/sql*",
        "--test-database-command",
        "postgres",
        "--test-database-ready-command",
        "pg_isready",
    )
    assert result.returncode == 0, result.stderr
    data = _installed_project_json(repo)
    runner = cast(dict[str, object], data["test_runner"])
    assert cast(dict[str, object], runner["build"])["artifact_paths"] == [
        "artifacts/**"
    ]
    assert cast(dict[str, object], runner["database"])["engine"] == "postgresql"
    health = subprocess.run(
        [sys.executable, str(CLI), "health", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert "test_runner" not in health.stdout + health.stderr


@pytest.mark.parametrize(
    "flags",
    [
        ["--test-build-command", "cc"],
        ["--test-artifact-path", "artifacts/**"],
        ["--test-database-engine", "postgresql"],
        ["--test-database-command", "postgres"],
    ],
)
def test_install_incomplete_groups_write_nothing(
    tmp_path: Path, flags: list[str]
) -> None:
    repo = _repo(tmp_path / "repo")
    result = _init(repo, *flags)
    assert result.returncode == 2
    assert not (repo / ".harness").exists()


def test_installed_runner_delivers_database_session_helper(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    assert _init(repo).returncode == 0
    assert (repo / ".harness/health/isolated_runner_session.py").is_file()


@pytest.mark.parametrize("invalid", [False, True])
def test_health_checks_optional_build_database_contract(
    tmp_path: Path, invalid: bool
) -> None:
    repo = _repo(tmp_path / "repo")
    assert (
        _init(
            repo, "--test-path", "tests/**", "--test-runner-command", "pytest {test_id}"
        ).returncode
        == 0
    )
    config = repo / ".harness/project.json"
    data = json.loads(config.read_text())
    data["test_runner"]["build"] = {
        "command": ["compiler"],
        "artifact_paths": [".build/**"],
    }
    data["test_runner"]["database"] = {
        "engine": "postgresql",
        "test_ids": ["sql.*"],
        "command": ["postgres"],
        "ready_command": [] if invalid else ["pg_isready"],
    }
    config.write_text(json.dumps(data))
    health = subprocess.run(
        [sys.executable, str(CLI), "health", str(repo), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    checks = json.loads(health.stdout)["checks"]
    result = next(item for item in checks if item["id"] == "files.project_json")
    assert result["status"] == ("fail" if invalid else "ok")
    if invalid:
        assert health.returncode != 0 and "ready_command" in result["message"]
