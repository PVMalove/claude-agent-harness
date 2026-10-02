"""Тесты заполнения поля tracker в .harness/project.json при установке харнесса из origin."""

from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.bin import harness as harness_cli
from harness.health.project_files import validate_project_json

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")
# Every non-tracker project.json value, so that only the tracker is left to prompt for.
PROJECT_FLAGS = [
    "--language",
    "ru",
    "--pr-base-branch",
    "main",
    "--branch-pattern",
    "^feature/.+",
    "--qa-gate-command",
    "echo test",
]
GITLAB_REMOTE = "https://ci-user@gitlab.example.test:4443/group/sub/project.git"
GITLAB_TRACKER = {
    "type": "gitlab",
    "host": "gitlab.example.test:4443",
    "project": "group/sub/project",
}
GITLAB_FLAGS = [
    "--tracker-type",
    "gitlab",
    "--tracker-host",
    "gitlab.example.test:4443",
    "--tracker-project",
    "group/sub/project",
]


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


def _harness(*args: str) -> subprocess.CompletedProcess[str]:
    """Запустить CLI харнесса без интерактивного ввода."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _install(repo: Path, *flags: str) -> None:
    """Установить pvmalove-suite без интерактивного ввода."""
    result = _harness("init", str(repo), "--capability", "pvmalove-suite", *flags)
    assert result.returncode == 0, result.stderr


class _CharacterDevice:
    """Заглушка stdin — символьное устройство, для которого `isatty()` истинен (консоль или NUL)."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0


def _install_interactively(
    repo: Path, answers: list[str], monkeypatch: pytest.MonkeyPatch, *flags: str
) -> list[str]:
    """Установить pvmalove-suite в терминале с ответами `answers`; вернуть заданные вопросы."""
    prompts: list[str] = []
    replies = iter(answers)

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(harness_cli, "_stdin_is_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    args = harness_cli.parser().parse_args(
        ["init", str(repo), "--capability", "pvmalove-suite", *PROJECT_FLAGS, *flags]
    )
    assert args.func(args) == 0
    assert next(replies, None) is None, "not every answer was asked for"
    return prompts


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


@pytest.mark.parametrize(("console_mode_result", "expected"), [(0, False), (1, True)])
def test_windows_stdin_is_a_terminal_only_for_a_console(
    monkeypatch: pytest.MonkeyPatch, console_mode_result: int, expected: bool
) -> None:
    """Проверить, что на Windows stdin NUL — не терминал, хотя `isatty()` для него истинен."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _CharacterDevice())
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 7)
    )
    kernel32 = SimpleNamespace(GetConsoleMode=lambda handle, mode: console_mode_result)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    assert harness_cli._stdin_is_terminal() is expected


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


def test_interactive_install_offers_the_origin_tracker_as_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что install в терминале предлагает тип, хост с портом и проект с подгруппами
    из GitLab-origin как дефолты и записывает подтверждённые значения."""
    repo = _repo(tmp_path / "repo", remote=GITLAB_REMOTE)

    prompts = _install_interactively(repo, ["", "", ""], monkeypatch)

    assert [prompt.split(" [")[1] for prompt in prompts] == [
        "gitlab]: ",
        "gitlab.example.test:4443]: ",
        "group/sub/project]: ",
    ]
    assert _installed_project_json(repo)["tracker"] == GITLAB_TRACKER
    assert not any("ci-user" in prompt for prompt in prompts)


def test_interactive_install_writes_the_answers_over_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что self-hosted GitLab без `gitlab.` в имени хоста, предложенный как local,
    записывается введённым типом с хостом и проектом из origin."""
    repo = _repo(
        tmp_path / "repo",
        remote="ssh://git@git.example.test:2222/group/sub/project.git",
    )

    prompts = _install_interactively(
        repo, ["gitlab", "git.example.test:4443", ""], monkeypatch
    )

    assert prompts[0].endswith("[local]: ")
    assert prompts[1].endswith("[git.example.test]: ")
    assert _installed_project_json(repo)["tracker"] == {
        "type": "gitlab",
        "host": "git.example.test:4443",
        "project": "group/sub/project",
    }


def test_interactive_install_without_origin_offers_and_writes_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что без распознаваемого origin install предлагает local и записывает его."""
    repo = _repo(tmp_path / "repo")

    prompts = _install_interactively(repo, [""], monkeypatch)

    assert prompts[0].endswith("[local]: ")
    assert _installed_project_json(repo)["tracker"] == {"type": "local"}


def test_interactive_install_asks_nothing_the_tracker_flags_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что в терминале все три флага трекера заполняют поле без единого вопроса."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")

    prompts = _install_interactively(repo, [], monkeypatch, *GITLAB_FLAGS)

    assert prompts == []
    assert _installed_project_json(repo)["tracker"] == GITLAB_TRACKER


def test_install_takes_no_origin_defaults_for_another_hosted_type(
    tmp_path: Path,
) -> None:
    """Проверить, что хост и проект GitLab-origin не подставляются в трекер типа github."""
    repo = _repo(tmp_path / "repo", remote=GITLAB_REMOTE)

    result = _harness(
        "init", str(repo), "--capability", "pvmalove-suite", "--tracker-type", "github"
    )

    assert result.returncode == 0, result.stderr
    assert "tracker field left out" in result.stderr
    assert "tracker" not in _installed_project_json(repo)


@pytest.mark.parametrize("remote", [None, "git@github.com:acme/widgets.git"])
def test_install_fills_the_tracker_field_from_flags_without_prompts(
    tmp_path: Path, remote: str | None
) -> None:
    """Проверить, что флаги задают поле tracker без вопросов и важнее origin."""
    repo = _repo(tmp_path / "repo", remote=remote)

    _install(repo, *GITLAB_FLAGS)

    assert _installed_project_json(repo)["tracker"] == GITLAB_TRACKER


def test_install_writes_a_local_tracker_given_by_flag(tmp_path: Path) -> None:
    """Проверить, что `--tracker-type local` записывает локальный трекер, которого нет по умолчанию."""
    repo = _repo(tmp_path / "repo")

    _install(repo, "--tracker-type", "local")

    assert _installed_project_json(repo)["tracker"] == {"type": "local"}


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--tracker-host", "https://ci-user:secret@gitlab.example.test"),
        ("--tracker-project", "/group/project/"),
        ("--tracker-type", "bitbucket"),
    ],
)
def test_install_rejects_an_invalid_tracker_flag_before_writing(
    tmp_path: Path, flag: str, value: str
) -> None:
    """Проверить, что некорректный флаг трекера отклоняется до записи файлов и не печатается."""
    repo = _repo(tmp_path / "repo")

    result = _harness("init", str(repo), "--capability", "pvmalove-suite", flag, value)

    assert result.returncode == 2
    assert flag in result.stderr
    assert "secret" not in result.stderr
    assert not (repo / ".harness").exists()


def test_update_with_tracker_flags_never_rewrites_project_json(tmp_path: Path) -> None:
    """Проверить, что update с флагами трекера не меняет уже созданный project.json."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _install(repo)
    project_json = repo / ".harness" / "project.json"
    installed = project_json.read_bytes()

    result = _harness("update", str(repo), *GITLAB_FLAGS)

    assert result.returncode == 0, result.stderr
    assert project_json.read_bytes() == installed


def test_install_leaves_out_an_incomplete_tracker_given_by_flags(
    tmp_path: Path,
) -> None:
    """Проверить, что тип gitlab без origin, хоста и проекта не пишется и install сообщает почему."""
    repo = _repo(tmp_path / "repo")

    result = _harness(
        "init", str(repo), "--capability", "pvmalove-suite", "--tracker-type", "gitlab"
    )

    assert result.returncode == 0, result.stderr
    assert "tracker field left out" in result.stderr
    assert "tracker" not in _installed_project_json(repo)
