"""Тесты заполнения поля tracker в .harness/project.json при установке харнесса из origin."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.health.project_files import validate_project_json

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")


def _repo(path: Path, *, remote: str | None = None) -> Path:
    """Создать git-репозиторий с опциональным origin."""
    path.mkdir(parents=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin", remote], cwd=path, check=True
        )
    return path


def _install(repo: Path) -> None:
    """Установить pvmalove-suite без интерактивного ввода, новых промптов и флагов."""
    result = subprocess.run(
        [sys.executable, str(CLI), "init", str(repo), "--capability", "pvmalove-suite"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _installed_project_json(repo: Path) -> dict[str, object]:
    """Прочитать установленный project.json и убедиться, что он проходит строгий контракт."""
    problems: list[str] = []
    validate_project_json(repo, problems)
    assert problems == []
    data = json.loads((repo / ".harness" / "project.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (
            "https://ci-user@gitlab.example.test:4443/group/sub/project.git",
            {
                "type": "gitlab",
                "host": "gitlab.example.test:4443",
                "project": "group/sub/project",
            },
        ),
        (
            "git@github.com:acme/widgets.git",
            {"type": "github", "host": "github.com", "project": "acme/widgets"},
        ),
    ],
)
def test_install_writes_the_tracker_field_derived_from_origin(
    tmp_path: Path, remote: str, expected: dict[str, str]
) -> None:
    """Проверить, что install заполняет поле tracker трекером GitHub/GitLab, выведенным из origin."""
    repo = _repo(tmp_path / "repo", remote=remote)

    _install(repo)

    data = _installed_project_json(repo)
    assert data["tracker"] == expected
    text = (repo / ".harness" / "project.json").read_text(encoding="utf-8")
    assert "ci-user" not in text


@pytest.mark.parametrize(
    "remote",
    [
        None,
        "https://git.example.test:4443/group/sub/project.git",
        "/srv/git/project.git",
    ],
)
def test_install_writes_no_tracker_field_for_a_local_or_default_tracker(
    tmp_path: Path, remote: str | None
) -> None:
    """Проверить, что для локального трекера и без origin поле tracker не пишется вовсе."""
    repo = _repo(tmp_path / "repo", remote=remote)

    _install(repo)

    assert "tracker" not in _installed_project_json(repo)


def test_install_never_rewrites_an_existing_project_json(tmp_path: Path) -> None:
    """Проверить, что существующий project.json — seed-файл — не переписывается установкой."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/sub/project.git",
    )
    existing = (
        json.dumps(
            {
                "language": "en",
                "base_branch": "main",
                "branch_pattern": "^feature/.+",
                "qa_gate_commands": ["echo test"],
            },
            indent=2,
        )
        + "\n"
    )
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.json").write_text(existing, encoding="utf-8")

    _install(repo)

    assert (repo / ".harness" / "project.json").read_text(encoding="utf-8") == existing
