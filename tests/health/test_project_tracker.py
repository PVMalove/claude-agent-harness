"""Тесты резолвера трекера проекта: разбор URL origin, поле tracker и расхождения между ними."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from harness.health import project_files
from harness.health.project_tracker import (
    ProjectTracker,
    RemoteLocation,
    TrackerType,
    parse_remote_url,
    resolve_project_tracker,
)

REAL_GIT = shutil.which("git")

_GITLAB_FIELD: dict[str, object] = {
    "type": "gitlab",
    "host": "gitlab.example.test:4443",
    "project": "group/sub/project",
}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://gitlab.example.test:4443/group/sub/project.git",
            RemoteLocation("gitlab.example.test", 4443, "group/sub/project", True),
        ),
        (
            "ssh://git@gitlab.example.test:2222/group/sub/project.git",
            RemoteLocation("gitlab.example.test", None, "group/sub/project", False),
        ),
        (
            "git@gitlab.example.test:group/sub/project.git",
            RemoteLocation("gitlab.example.test", None, "group/sub/project", False),
        ),
        (
            "https://gitlab.example.test/group/sub/project.git",
            RemoteLocation("gitlab.example.test", None, "group/sub/project", True),
        ),
        (
            "https://gitlab.example.test/group/sub/project.name.git",
            RemoteLocation("gitlab.example.test", None, "group/sub/project.name", True),
        ),
        (
            "https://ci-user@gitlab.example.test:4443/group/sub/project.git",
            RemoteLocation("gitlab.example.test", 4443, "group/sub/project", True),
        ),
        (
            "https://GitLab.Example.Test:443/group/sub/project/",
            RemoteLocation("gitlab.example.test", None, "group/sub/project", True),
        ),
        (
            "http://git.example.test:80/group/project",
            RemoteLocation("git.example.test", None, "group/project", True),
        ),
        (
            "git+ssh://git@git.example.test/group/project.git",
            RemoteLocation("git.example.test", None, "group/project", False),
        ),
        (
            "git@github.com:acme/widgets.git",
            RemoteLocation("github.com", None, "acme/widgets", False),
        ),
        (
            "https://github.com/acme/widgets.js.git",
            RemoteLocation("github.com", None, "acme/widgets.js", True),
        ),
        (
            "ssh://git@ssh.github.com:443/acme/widgets.git",
            RemoteLocation("ssh.github.com", None, "acme/widgets", False),
        ),
        (
            "https://github.com/acme",
            RemoteLocation("github.com", None, None, True),
        ),
        (
            "https://gitlab.example.test/group//project.git",
            RemoteLocation("gitlab.example.test", None, None, True),
        ),
    ],
)
def test_parse_remote_url_reads_host_port_and_the_full_project_path(
    url: str, expected: RemoteLocation
) -> None:
    """Проверить разбор https://, ssh://, SCP-формы, userinfo, порта, подгрупп и точки в имени."""
    assert parse_remote_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "/srv/git/project.git",
        "./project",
        "../project",
        "~/project.git",
        "C:\\repos\\project",
        "C:/repos/project",
        "file:///srv/git/project.git",
        "https://gitlab.example.test:notaport/group/project.git",
        "https:///group/project.git",
        "project",
    ],
)
def test_parse_remote_url_rejects_local_paths_and_unsupported_urls(url: str) -> None:
    """Проверить, что локальные пути, file:// и неразбираемые URL не дают RemoteLocation."""
    assert parse_remote_url(url) is None


def test_remote_location_host_keeps_only_a_known_port() -> None:
    """Проверить, что host содержит порт только тогда, когда он явно задан и не дефолтный."""
    assert RemoteLocation("gitlab.example.test", 4443, None, True).host == (
        "gitlab.example.test:4443"
    )
    assert RemoteLocation("gitlab.example.test", None, None, False).host == (
        "gitlab.example.test"
    )


def test_tracker_type_literal_matches_the_validator_types() -> None:
    """Проверить, что Literal TrackerType совпадает с перечнем типов валидатора поля tracker."""
    assert get_args(TrackerType) == project_files.TRACKER_TYPES


def test_snippet_is_a_ready_to_paste_tracker_entry() -> None:
    """Проверить, что сниппет — готовая к вставке запись "tracker" без неизвестных ключей."""
    gitlab = ProjectTracker(
        "gitlab", "gitlab.example.test:4443", "group/sub/project", "origin"
    )
    local = ProjectTracker("local", None, None, "default")

    assert json.loads("{" + gitlab.snippet() + "}") == {"tracker": _GITLAB_FIELD}
    assert local.snippet() == '"tracker": {"type": "local"}'
    assert project_files.tracker_field_problems(gitlab.field()) == []
    assert project_files.tracker_field_problems(local.field()) == []


# --- resolve_project_tracker on temporary repositories ------------------------------------------


def _repo(path: Path, *, remote: str | None = None, tracker: object = None) -> Path:
    """Создать git-репозиторий с опциональным origin и опциональным полем tracker в project.json."""
    path.mkdir(parents=True, exist_ok=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin", remote], cwd=path, check=True
        )
    if tracker is not None:
        _project_json(path, {"tracker": tracker})
    return path


def _project_json(repo: Path, extra: dict[str, object]) -> None:
    """Записать .harness/project.json с обязательными полями и дополнительными ключами."""
    data: dict[str, object] = {
        "language": "ru",
        "base_branch": "main",
        "branch_pattern": "^feature/.+",
        "qa_gate_commands": ["echo test"],
        **extra,
    }
    path = repo / ".harness" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_without_project_json_the_tracker_comes_from_origin(tmp_path: Path) -> None:
    """Проверить, что без project.json трекер выводится из origin с источником origin."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/sub/project.git",
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.project_json == "absent"
    assert resolution.declared is None
    assert resolution.effective == ProjectTracker(
        "gitlab", "gitlab.example.test:4443", "group/sub/project", "origin"
    )
    assert resolution.mismatches == ()


def test_without_origin_and_field_the_source_is_default(tmp_path: Path) -> None:
    """Проверить, что без поля и без origin трекер локальный с источником default."""
    repo = _repo(tmp_path / "repo")
    _project_json(repo, {})

    resolution = resolve_project_tracker(repo)

    assert resolution.project_json == "no_field"
    assert resolution.origin is None
    assert resolution.effective == ProjectTracker("local", None, None, "default")


def test_an_unparsable_origin_is_local_from_origin(tmp_path: Path) -> None:
    """Проверить, что неразбираемый origin даёт локальный трекер с источником origin."""
    repo = _repo(tmp_path / "repo", remote="/srv/git/project.git")

    resolution = resolve_project_tracker(repo)

    assert resolution.origin is None
    assert resolution.effective == ProjectTracker("local", None, None, "origin")


def test_github_origin_is_normalized_to_github_com(tmp_path: Path) -> None:
    """Проверить, что поддомен github.com нормализуется в хост github.com."""
    repo = _repo(
        tmp_path / "repo", remote="ssh://git@ssh.github.com:443/acme/widgets.git"
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective == ProjectTracker(
        "github", "github.com", "acme/widgets", "origin"
    )


def test_a_host_merely_containing_github_com_is_not_github(tmp_path: Path) -> None:
    """Проверить, что хост, лишь содержащий подстроку github.com, не считается GitHub."""
    repo = _repo(
        tmp_path / "repo", remote="https://notgithub.com.example.test/acme/widgets.git"
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective.type == "local"


def test_host_without_gitlab_in_its_name_and_without_field_resolves_to_local(
    tmp_path: Path,
) -> None:
    """Проверить, что хост без gitlab. в имени без поля даёт local, сохраняя хост и проект."""
    repo = _repo(
        tmp_path / "repo", remote="https://git.example.test:4443/group/sub/project.git"
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective == ProjectTracker(
        "local", "git.example.test:4443", "group/sub/project", "origin"
    )


def test_host_without_gitlab_in_its_name_with_field_resolves_to_gitlab(
    tmp_path: Path,
) -> None:
    """Проверить, что тот же хост с полем tracker даёт gitlab из поля без расхождений."""
    field = {
        "type": "gitlab",
        "host": "git.example.test:4443",
        "project": "group/sub/project",
    }
    repo = _repo(
        tmp_path / "repo",
        remote="https://git.example.test:4443/group/sub/project.git",
        tracker=field,
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.project_json == "valid"
    assert resolution.effective == ProjectTracker(
        "gitlab", "git.example.test:4443", "group/sub/project", "config"
    )
    assert resolution.from_origin.type == "local"
    assert resolution.mismatches == ()


def test_a_valid_field_wins_over_a_disagreeing_origin(tmp_path: Path) -> None:
    """Проверить, что при расхождении с origin побеждает поле, а расхождение фиксируется."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/other.git",
        tracker=_GITLAB_FIELD,
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective.project == "group/sub/project"
    assert resolution.effective.source == "config"
    assert resolution.from_origin.project == "group/other"
    assert resolution.mismatches == ("project",)


def test_type_mismatch_between_github_origin_and_gitlab_field(tmp_path: Path) -> None:
    """Проверить, что GitHub-origin при поле gitlab даёт расхождение по типу и хосту."""
    repo = _repo(
        tmp_path / "repo",
        remote="git@github.com:group/sub/project.git",
        tracker=_GITLAB_FIELD,
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective.type == "gitlab"
    assert resolution.mismatches == ("type", "host")


def test_host_mismatch_is_reported(tmp_path: Path) -> None:
    """Проверить, что другой хост в origin даёт расхождение по хосту."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.other.example.test/group/sub/project.git",
        tracker=_GITLAB_FIELD,
    )

    assert resolve_project_tracker(repo).mismatches == ("host",)


def test_https_port_mismatch_is_reported_but_default_port_is_not(
    tmp_path: Path,
) -> None:
    """Проверить, что порт https сравнивается, а :443 в поле равен отсутствию порта."""
    other_port = _repo(
        tmp_path / "other-port",
        remote="https://gitlab.example.test:8443/group/sub/project.git",
        tracker=_GITLAB_FIELD,
    )
    default_port = _repo(
        tmp_path / "default-port",
        remote="https://gitlab.example.test/group/sub/project.git",
        tracker={**_GITLAB_FIELD, "host": "gitlab.example.test:443"},
    )

    assert resolve_project_tracker(other_port).mismatches == ("host",)
    assert resolve_project_tracker(default_port).mismatches == ()


def test_ssh_origin_with_a_port_in_the_field_is_not_a_mismatch(tmp_path: Path) -> None:
    """Проверить, что SSH-порт origin не сравнивается с веб-портом поля."""
    repo = _repo(
        tmp_path / "repo",
        remote="ssh://git@gitlab.example.test:2222/group/sub/project.git",
        tracker=_GITLAB_FIELD,
    )

    assert resolve_project_tracker(repo).mismatches == ()


def test_local_field_with_github_origin_is_not_a_mismatch(tmp_path: Path) -> None:
    """Проверить, что поле local при GitHub-origin — законная конфигурация без расхождения."""
    repo = _repo(
        tmp_path / "repo",
        remote="git@github.com:acme/widgets.git",
        tracker={"type": "local"},
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.effective == ProjectTracker("local", None, None, "config")
    assert resolution.mismatches == ()


def test_an_invalid_field_does_not_win(tmp_path: Path) -> None:
    """Проверить, что некорректное поле tracker не побеждает: трекер выводится из origin."""
    repo = _repo(
        tmp_path / "repo",
        remote="git@gitlab.example.test:group/sub/project.git",
        tracker={**_GITLAB_FIELD, "unexpected": True},
    )

    resolution = resolve_project_tracker(repo)

    assert resolution.project_json == "invalid"
    assert resolution.declared is None
    assert resolution.effective == ProjectTracker(
        "gitlab", "gitlab.example.test", "group/sub/project", "origin"
    )


def test_unreadable_project_json_is_invalid(tmp_path: Path) -> None:
    """Проверить, что нечитаемый project.json даёт состояние invalid."""
    repo = _repo(tmp_path / "repo")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.json").write_text("{not json", encoding="utf-8")

    assert resolve_project_tracker(repo).project_json == "invalid"


def test_the_raw_origin_url_never_reaches_the_resolution(tmp_path: Path) -> None:
    """Проверить, что userinfo из URL origin не попадает в результат резолвера."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://ci-user@gitlab.example.test:4443/group/sub/project.git",
    )

    resolution = resolve_project_tracker(repo)

    assert "ci-user" not in repr(resolution)
    assert resolution.effective.host == "gitlab.example.test:4443"
