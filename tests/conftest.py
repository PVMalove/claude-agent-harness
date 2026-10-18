"""Shared fixtures for the per-language real-bundle Repo Map tests (ADR 0008, #279).

`tests/test_repo_map_tree_sitter.py` keeps its own inline helpers (Python/TS/JS, #277); the
per-language files added by #279 (Go, Java, C#, and the unsupported-language regression) share
these instead of repeating them.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.repo_map import repo_map

BUNDLE_ENV = "HARNESS_PARSER_BUNDLE_DIR"


def find_bash() -> str:
    """Путь к рабочему bash.

    На Windows голый `bash` в PATH может указывать на заглушку WSL в System32,
    которая без установленного дистрибутива завершается ошибкой. Поэтому на Windows
    ищется реальный bash из поставки Git for Windows.
    """
    if sys.platform != "win32":
        return "bash"
    git_exe = shutil.which("git")
    if git_exe:
        for ancestor in Path(git_exe).resolve().parents:
            for candidate in (
                ancestor / "bin" / "bash.exe",
                ancestor / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)
    for root in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles(x86)"),
    ):
        if root:
            for candidate in (
                Path(root) / "Git" / "bin" / "bash.exe",
                Path(root) / "Git" / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)
    return "bash"


def _git(repo: Path, *args: str) -> str:
    """Выполнить команду git в указанном репозитории и вернуть вывод."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def bundle_dir() -> Path:
    """Фикстура для получения пути к каталогу с бандлом парсеров из переменной окружения."""
    configured = os.environ.get(BUNDLE_ENV)
    if not configured:
        pytest.skip(f"{BUNDLE_ENV} is not set; the bundle CI job runs these tests")
    path = Path(configured)
    if not (path / "parser_bundle.lock.json").is_file():
        pytest.fail(f"{BUNDLE_ENV}={configured} has no parser_bundle.lock.json")
    return path


def build_map(
    tmp_path: Path, bundle: Path, files: dict[str, str], **policy: object
) -> tuple[bytes, dict[str, object]]:
    """Собрать карту репозитория для заданного набора файлов и вернуть закодированное и декодированное представления."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    policy_path = tmp_path / "orchestration.json"
    policy_path.write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "full",
                    "parser_bundle_registry_paths": [str(bundle)],
                    "parser_bundle_timeout_seconds": 120,
                    **policy,
                }
            }
        )
    )
    loaded = repo_map.load_policy(policy_path, explicit=True)
    encoded = repo_map.build_map(repo, commit, 8000, [], loaded)
    assert repo_map.build_map(repo, commit, 8000, [], loaded) == encoded
    decoded: dict[str, object] = json.loads(encoded)
    return encoded.encode("utf-8"), decoded


def records(result: dict[str, object], key: str) -> list[dict[str, object]]:
    """Извлечь список записей по ключу из результата построения карты."""
    values = result[key]
    assert isinstance(values, list)
    return values


def file_record(result: dict[str, object], path: str) -> dict[str, object]:
    """Найти запись для указанного файла в результатах построения карты."""
    return next(item for item in records(result, "files") if item["path"] == path)
