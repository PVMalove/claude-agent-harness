"""Сквозные тесты сетевых проверок GitHub/GitLab трекера группы 'tracker'."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")

_FAKE_TOOL = """
import json, subprocess, sys
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
args = sys.argv[2:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
key = list(args)
while len(key) >= 2 and key[0] in ("-c", "-C"):
    key = key[2:]
response = spec["responses"].get(" ".join(key))
if response is not None:
    code, out, err = response
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)
if spec["passthrough"]:
    sys.exit(subprocess.run([spec["passthrough"], *args]).returncode)
sys.stderr.write("fake tool: unexpected arguments " + " ".join(args) + "\\n")
sys.exit(2)
"""

Responses = dict[str, tuple[int, str, str]]


def _fake_tool(
    bin_dir: Path, name: str, responses: Responses, *, passthrough: str | None = None
) -> Path:
    """Создать фиктивную утилиту с имитацией ответов и логированием вызовов."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / f"{name}.log"
    spec = bin_dir / f"{name}.json"
    spec.write_text(
        json.dumps(
            {"responses": responses, "passthrough": passthrough, "log": str(log)}
        ),
        encoding="utf-8",
    )
    script = bin_dir / f"{name}_fake.py"
    script.write_text(_FAKE_TOOL, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(
            f'@"{sys.executable}" "{script}" "{spec}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = bin_dir / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{spec}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return log


def _invocations(log: Path) -> list[list[str]]:
    """Прочитать список зарегистрированных вызовов фиктивной утилиты из лога."""
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _fake_git(bin_dir: Path, responses: Responses | None = None) -> Path:
    """Создать фиктивную утилиту git с заданными ответами."""
    canned: Responses = {"--version": (0, "git version 9.9.9\n", "")}
    canned.update(responses or {})
    assert REAL_GIT is not None
    return _fake_tool(bin_dir, "git", canned, passthrough=REAL_GIT)


def _fake_gh(bin_dir: Path, responses: Responses | None = None) -> Path:
    """Создать фиктивную утилиту gh с заданными ответами."""
    canned: Responses = {"auth status --hostname github.com": (0, "", "")}
    canned.update(responses or {})
    return _fake_tool(bin_dir, "gh", canned)


def _fake_glab(
    bin_dir: Path,
    responses: Responses | None = None,
    *,
    host: str = "gitlab.example.com",
) -> Path:
    """Создать фиктивную утилиту glab, аутентифицированную только на host, с заданными ответами."""
    canned: Responses = {f"auth status --hostname {host}": (0, "", "")}
    canned.update(responses or {})
    return _fake_tool(bin_dir, "glab", canned)


def _gitlab_access(host: str, encoded_project: str, access_level: int) -> Responses:
    """Ответы glab api: текущий пользователь и его эффективный уровень доступа в проекте."""
    return {
        f"api --hostname {host} user": (
            0,
            json.dumps({"id": 7, "username": "dev"}),
            "",
        ),
        f"api --hostname {host} projects/{encoded_project}/members/all/7": (
            0,
            json.dumps({"id": 7, "username": "dev", "access_level": access_level}),
            "",
        ),
    }


def _repo(path: Path, *, remote: str | None = None) -> Path:
    """Создать и инициализировать git-репозиторий с опциональным remote."""
    path.mkdir(parents=True, exist_ok=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin", remote], cwd=path, check=True
        )
    return path


def _triage_labels(repo: Path) -> None:
    """Создать эталонный файл triage-labels.md в репозитории."""
    (repo / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "agents" / "triage-labels.md").write_text(
        "\n".join(
            [
                "| Label | Color | Meaning |",
                "| --- | --- | --- |",
                "| `hitl` | yellow `#fbca04` | Human-in-the-loop |",
                "| `afk` | light blue `#54c1e8` | Away-from-keyboard |",
                "| `status::ready` | green `#0e8a16` | Ready |",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _health(
    repo: Path, *bin_dirs: Path, online: bool = False, fix: bool = False
) -> tuple[int, dict[str, dict[str, object]]]:
    """Выполнить health --json с указанными каталогами утилит и вернуть код и словарь проверок."""
    _exit_code, _checks, _data = _health_full(repo, *bin_dirs, online=online, fix=fix)
    return _exit_code, _checks


def _health_full(
    repo: Path, *bin_dirs: Path, online: bool = False, fix: bool = False
) -> tuple[int, dict[str, dict[str, object]], dict[str, object]]:
    """Выполнить health --json с полным возвратом кода завершения, словаря проверок и сырых данных."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONIOENCODING", "PYTHONUTF8", "GIT_DIR", "GIT_WORK_TREE")
    }
    env["PATH"] = os.pathsep.join(str(path) for path in bin_dirs)
    env["PYTHONUTF8"] = "1"
    argv = [sys.executable, str(HARNESS), "health", str(repo), "--json"]
    if online:
        argv.append("--online")
    if fix:
        argv.append("--fix")
    result = subprocess.run(argv, env=env, capture_output=True, check=False)
    data = json.loads(result.stdout.decode("utf-8"))
    return (
        result.returncode,
        {check["id"]: check for check in data["checks"]},
        data,
    )


TRACKER_IDS = (
    "tracker.auth",
    "tracker.reachability",
    "tracker.permissions",
    "tracker.labels",
)


# --- offline (default) ---------------------------------------------------------------------------


def test_without_online_every_tracker_check_is_skipped_offline(tmp_path: Path) -> None:
    """Проверить, что без флага online все проверки трекера пропускаются со статусом офлайн."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=False)

    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "офлайн" in str(checks[check_id]["message"])


# --- local tracker ---------------------------------------------------------------------------------


def test_online_with_a_non_hosted_remote_is_skipped_as_local(tmp_path: Path) -> None:
    """Проверить, что удаленный репозиторий без хостинга на GitHub/GitLab пропускается как локальный."""
    repo = _repo(tmp_path / "repo", remote="https://example.internal/team/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "локальный" in str(checks[check_id]["message"])


def test_online_with_no_remote_at_all_is_skipped_as_local(tmp_path: Path) -> None:
    """Проверить, что при полном отсутствии remote проверка пропускается как локальный трекер."""
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "skipped"


# --- missing gh ------------------------------------------------------------------------------------


def test_online_github_without_gh_warns_and_skips_dependents(tmp_path: Path) -> None:
    """Проверить, что отсутствие утилиты gh приводит к предупреждению и пропуску зависимых проверок."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    exit_code, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "warn"
    assert "gh" in str(checks["tracker.auth"]["message"])
    assert checks["tracker.permissions"]["status"] == "warn"
    assert checks["tracker.labels"]["status"] == "warn"
    # reachability does not depend on gh, only on git, which is present.
    assert checks["tracker.reachability"]["status"] in ("ok", "fail")
    assert exit_code in (0, 1)


# --- gh auth status ---------------------------------------------------------------------------------


def test_online_github_gh_not_authenticated_fails_auth_and_skips_dependents(
    tmp_path: Path,
) -> None:
    """Проверить, что неаутентифицированный gh приводит к ошибке авторизации."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir, {"auth status --hostname github.com": (1, "", "not logged in")}
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "fail"
    # the raw (possibly sensitive) stderr of `gh auth status` never reaches the report.
    assert "not logged in" not in str(checks["tracker.auth"]["message"])
    invocations = _invocations(gh_log)
    assert ["auth", "status", "--hostname", "github.com"] in invocations


def test_online_github_authenticated_reports_ok(tmp_path: Path) -> None:
    """Проверить, что успешно аутентифицированный GitHub сообщает статус ok."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"admin": True, "push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                "[]",
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "ok"
    assert checks["tracker.reachability"]["status"] == "ok"


# --- permissions -----------------------------------------------------------------------------------


def test_online_github_triage_only_permissions_warns_about_pull_requests(
    tmp_path: Path,
) -> None:
    """Проверить, что права только уровня triage вызывают предупреждение о недоступности PR."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": False, "triage": True}),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    permissions = checks["tracker.permissions"]
    assert permissions["status"] == "warn"
    assert "недостаточно прав для PR" in str(permissions["message"])
    assert "метки доступны" in str(permissions["message"])


def test_online_gitlab_developer_access_level_has_full_permissions(
    tmp_path: Path,
) -> None:
    """Проверить, что уровень доступа developer в GitLab обладает полными правами."""
    repo = _repo(tmp_path / "repo", remote="git@gitlab.example.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_glab(
        bin_dir,
        _gitlab_access("gitlab.example.com", "acme%2Fwidgets", 30),
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.permissions"]["status"] == "ok"


def test_online_gitlab_reporter_access_level_can_manage_labels_only(
    tmp_path: Path,
) -> None:
    """Проверить, что уровень доступа reporter в GitLab позволяет управлять только метками."""
    repo = _repo(tmp_path / "repo", remote="git@gitlab.example.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_glab(
        bin_dir,
        _gitlab_access("gitlab.example.com", "acme%2Fwidgets", 20),
    )

    _, checks = _health(repo, bin_dir, online=True)

    permissions = checks["tracker.permissions"]
    assert permissions["status"] == "warn"
    assert "недостаточно прав для PR" in str(permissions["message"])
    assert "метки доступны" in str(permissions["message"])


def test_online_gitlab_developer_through_a_parent_group_has_push_permissions(
    tmp_path: Path,
) -> None:
    """Проверить, что Developer, получивший доступ через родительскую группу, получает ok по правам,
    хотя прямых прав на проект и его группу в projects/:id нет."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/sub/project.git",
    )
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    host = "gitlab.example.test:4443"
    responses = _gitlab_access(host, "group%2Fsub%2Fproject", 30)
    responses[f"api --hostname {host} projects/group%2Fsub%2Fproject"] = (
        0,
        json.dumps({"permissions": {"project_access": None, "group_access": None}}),
        "",
    )
    glab_log = _fake_glab(bin_dir, responses, host=host)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.permissions"]["status"] == "ok"
    assert [
        "api",
        "--hostname",
        host,
        "projects/group%2Fsub%2Fproject/members/all/7",
    ] in _invocations(glab_log)


def test_online_gitlab_auth_is_checked_for_exactly_the_tracker_host(
    tmp_path: Path,
) -> None:
    """Проверить, что glab, аутентифицированный только на другом хосте, даёт fail авторизации, а
    auth status вызывается с --hostname хоста трекера проекта."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/sub/project.git",
    )
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    glab_log = _fake_glab(bin_dir, host="gitlab.example.com")

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "fail"
    assert "gitlab.example.test:4443" in str(checks["tracker.auth"]["message"])
    assert ["auth", "status", "--hostname", "gitlab.example.test:4443"] in _invocations(
        glab_log
    )


@pytest.mark.parametrize(
    ("remote", "host", "encoded_project"),
    [
        (
            "https://gitlab.example.test:4443/group/sub/project.git",
            "gitlab.example.test:4443",
            "group%2Fsub%2Fproject",
        ),
        (
            "ssh://git@gitlab.example.test:2222/group/sub/project.git",
            "gitlab.example.test",
            "group%2Fsub%2Fproject",
        ),
        (
            "git@gitlab.example.test:group/sub/project.git",
            "gitlab.example.test",
            "group%2Fsub%2Fproject",
        ),
        (
            "https://gitlab.example.test/group/sub/project.name.git",
            "gitlab.example.test",
            "group%2Fsub%2Fproject.name",
        ),
        (
            "https://ci-user@gitlab.example.test:4443/group/sub/project.git",
            "gitlab.example.test:4443",
            "group%2Fsub%2Fproject",
        ),
    ],
)
def test_online_gitlab_origin_forms_address_the_full_project_path(
    tmp_path: Path, remote: str, host: str, encoded_project: str
) -> None:
    """Проверить, что HTTPS с портом, ssh:// с портом, SCP-форма, точка в имени и userinfo дают
    GitLab, glab api с --hostname хоста трекера и полный путь проекта с подгруппами."""
    repo = _repo(tmp_path / "repo", remote=remote)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    glab_log = _fake_glab(bin_dir, _gitlab_access(host, encoded_project, 30), host=host)

    _, checks, data = _health_full(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "ok"
    assert checks["tracker.permissions"]["status"] == "ok"
    invocations = _invocations(glab_log)
    assert ["auth", "status", "--hostname", host] in invocations
    assert [
        "api",
        "--hostname",
        host,
        f"projects/{encoded_project}/members/all/7",
    ] in invocations
    assert "ci-user" not in json.dumps(data, ensure_ascii=False)


_SELF_HOSTED_ORIGIN = "https://git.example.test:4443/group/sub/project.git"


def _project_json(repo: Path, tracker: dict[str, str] | None = None) -> None:
    """Записать .harness/project.json с обязательными полями и опциональным полем tracker."""
    data: dict[str, object] = {
        "language": "ru",
        "base_branch": "main",
        "branch_pattern": "^feature/.+",
        "qa_gate_commands": ["echo test"],
    }
    if tracker is not None:
        data["tracker"] = tracker
    (repo / ".harness").mkdir(parents=True, exist_ok=True)
    (repo / ".harness" / "project.json").write_text(json.dumps(data), encoding="utf-8")


def test_online_host_without_gitlab_in_its_name_without_field_is_local(
    tmp_path: Path,
) -> None:
    """Проверить, что хост без gitlab. в имени без поля tracker — локальный трекер без вызовов glab."""
    repo = _repo(tmp_path / "repo", remote=_SELF_HOSTED_ORIGIN)
    _project_json(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    glab_log = _fake_glab(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "локальный" in str(checks[check_id]["message"])
    assert _invocations(glab_log) == []


def test_online_host_without_gitlab_in_its_name_with_field_is_gitlab(
    tmp_path: Path,
) -> None:
    """Проверить, что тот же хост с полем tracker type gitlab проверяется через glab по полному пути."""
    repo = _repo(tmp_path / "repo", remote=_SELF_HOSTED_ORIGIN)
    _project_json(
        repo,
        {
            "type": "gitlab",
            "host": "git.example.test:4443",
            "project": "group/sub/project",
        },
    )
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    host = "git.example.test:4443"
    glab_log = _fake_glab(
        bin_dir, _gitlab_access(host, "group%2Fsub%2Fproject", 30), host=host
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["files.project_json"]["status"] == "ok"
    assert checks["tracker.auth"]["status"] == "ok"
    assert checks["tracker.permissions"]["status"] == "ok"
    assert [
        "api",
        "--hostname",
        host,
        "projects/group%2Fsub%2Fproject/members/all/7",
    ] in _invocations(glab_log)


# --- tracker.project (offline) ---------------------------------------------------------------------

_GITLAB_FIELD = {
    "type": "gitlab",
    "host": "gitlab.example.test:4443",
    "project": "group/sub/project",
}


def _snippet(fix: object) -> dict[str, object]:
    """Разобрать сниппет из текста fix как JSON-фрагмент "tracker": {...}."""
    assert isinstance(fix, dict)
    assert fix["command"] is None
    text = str(fix["text"])
    prefix = "добавьте в .harness/project.json: "
    assert text.startswith(prefix)
    snippet = text[len(prefix) :].split("; ", 1)[0]
    parsed = json.loads("{" + snippet + "}")
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    ("remote", "description"),
    [
        (
            "https://gitlab.example.test:4443/group/sub/project.git",
            "gitlab, хост gitlab.example.test:4443, проект group/sub/project",
        ),
        (
            "ssh://git@gitlab.example.test:2222/group/sub/project.git",
            "gitlab, хост gitlab.example.test, проект group/sub/project",
        ),
        (
            "git@gitlab.example.test:group/sub/project.git",
            "gitlab, хост gitlab.example.test, проект group/sub/project",
        ),
        (
            "https://gitlab.example.test/group/sub/project.git",
            "gitlab, хост gitlab.example.test, проект group/sub/project",
        ),
        (
            "https://gitlab.example.test/group/sub/project.name.git",
            "gitlab, хост gitlab.example.test, проект group/sub/project.name",
        ),
        (
            "https://ci-user@gitlab.example.test:4443/group/sub/project.git",
            "gitlab, хост gitlab.example.test:4443, проект group/sub/project",
        ),
    ],
)
def test_project_reports_gitlab_and_the_full_path_offline(
    tmp_path: Path, remote: str, description: str
) -> None:
    """Проверить, что health --json без --online показывает gitlab и полный путь проекта для
    HTTPS с портом, ssh:// с портом, SCP-формы, подгрупп, точки в имени и userinfo."""
    repo = _repo(tmp_path / "repo", remote=remote)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks, data = _health_full(repo, bin_dir, online=False)

    project = checks["tracker.project"]
    assert project["status"] == "ok"
    assert project["message"] == (
        f"трекер проекта: {description} (источник: origin); .harness/project.json отсутствует"
    )
    assert "ci-user" not in json.dumps(data, ensure_ascii=False)


def test_project_for_github_origin_without_project_json_is_ok(tmp_path: Path) -> None:
    """Проверить, что GitHub-origin без project.json даёт ok с тройкой github и источником origin."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    assert checks["tracker.project"]["status"] == "ok"
    assert checks["tracker.project"]["message"] == (
        "трекер проекта: github, хост github.com, проект acme/widgets (источник: origin); "
        ".harness/project.json отсутствует"
    )


def test_project_without_origin_and_project_json_is_local_default(
    tmp_path: Path,
) -> None:
    """Проверить, что без origin и project.json трекер локальный с источником «нет origin»."""
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    assert checks["tracker.project"]["message"] == (
        "трекер проекта: local (источник: нет origin); .harness/project.json отсутствует"
    )


def test_project_without_field_warns_with_a_gitlab_snippet(tmp_path: Path) -> None:
    """Проверить, что без поля tracker выдаётся warn с готовым к вставке сниппетом GitLab."""
    repo = _repo(
        tmp_path / "repo",
        remote="https://gitlab.example.test:4443/group/sub/project.git",
    )
    _project_json(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    project = checks["tracker.project"]
    assert project["status"] == "warn"
    assert project["message"] == (
        "нет поля tracker в .harness/project.json; трекер проекта: gitlab, хост "
        "gitlab.example.test:4443, проект group/sub/project (источник: origin)"
    )
    assert _snippet(project["fix"]) == {"tracker": _GITLAB_FIELD}
    assert checks["files.project_json"]["status"] == "ok"


def test_project_without_field_warns_with_a_github_snippet(tmp_path: Path) -> None:
    """Проверить, что для GitHub-origin сниппет содержит github, github.com и owner/repo."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _project_json(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    project = checks["tracker.project"]
    assert project["status"] == "warn"
    assert _snippet(project["fix"]) == {
        "tracker": {"type": "github", "host": "github.com", "project": "acme/widgets"}
    }
    assert "SSH" not in str(project["fix"])


def test_project_without_field_for_a_host_without_gitlab_suggests_gitlab(
    tmp_path: Path,
) -> None:
    """Проверить, что для хоста без gitlab. сниппет local подсказывает замену на gitlab."""
    repo = _repo(tmp_path / "repo", remote=_SELF_HOSTED_ORIGIN)
    _project_json(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    project = checks["tracker.project"]
    assert project["status"] == "warn"
    assert (
        "трекер проекта: local, хост git.example.test:4443, проект group/sub/project"
        in str(project["message"])
    )
    assert _snippet(project["fix"]) == {
        "tracker": {
            "type": "local",
            "host": "git.example.test:4443",
            "project": "group/sub/project",
        }
    }
    assert 'замените "local" на "gitlab"' in str(project["fix"])


def test_project_without_field_for_an_ssh_origin_asks_for_the_web_port(
    tmp_path: Path,
) -> None:
    """Проверить, что для SSH-origin подсказка просит дописать веб-порт в host."""
    repo = _repo(
        tmp_path / "repo", remote="git@gitlab.example.test:group/sub/project.git"
    )
    _project_json(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    fix = checks["tracker.project"]["fix"]
    assert _snippet(fix) == {
        "tracker": {
            "type": "gitlab",
            "host": "gitlab.example.test",
            "project": "group/sub/project",
        }
    }
    assert "origin задан по SSH" in str(fix)


def test_project_with_a_matching_field_is_ok_from_the_field(tmp_path: Path) -> None:
    """Проверить, что хост без gitlab. с полем tracker даёт ok из поля без расхождения."""
    repo = _repo(tmp_path / "repo", remote=_SELF_HOSTED_ORIGIN)
    _project_json(
        repo,
        {
            "type": "gitlab",
            "host": "git.example.test:4443",
            "project": "group/sub/project",
        },
    )
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir)

    assert checks["tracker.project"]["status"] == "ok"
    assert checks["tracker.project"]["message"] == (
        "трекер проекта: gitlab, хост git.example.test:4443, проект group/sub/project "
        "(источник: поле tracker)"
    )


def test_project_field_disagreeing_with_origin_warns_and_the_field_wins(
    tmp_path: Path,
) -> None:
    """Проверить, что при расхождении поля и origin выдаётся warn, а glab адресует проект из поля."""
    repo = _repo(
        tmp_path / "repo", remote="https://gitlab.example.test:4443/group/other.git"
    )
    _project_json(repo, _GITLAB_FIELD)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    host = "gitlab.example.test:4443"
    glab_log = _fake_glab(
        bin_dir, _gitlab_access(host, "group%2Fsub%2Fproject", 30), host=host
    )

    _, checks = _health(repo, bin_dir, online=True)

    project = checks["tracker.project"]
    assert project["status"] == "warn"
    assert project["message"] == (
        "поле tracker расходится с origin (проект): используется поле — gitlab, хост "
        "gitlab.example.test:4443, проект group/sub/project; origin — gitlab, хост "
        "gitlab.example.test:4443, проект group/other"
    )
    assert isinstance(project["fix"], dict) and project["fix"]["command"] is None
    assert checks["tracker.permissions"]["status"] == "ok"
    invocations = _invocations(glab_log)
    assert [
        "api",
        "--hostname",
        host,
        "projects/group%2Fsub%2Fproject/members/all/7",
    ] in invocations
    assert not any("group%2Fother" in arg for call in invocations for arg in call)


def test_project_with_an_invalid_field_warns_that_it_is_not_applied(
    tmp_path: Path,
) -> None:
    """Проверить, что некорректное поле tracker не применяется и health об этом предупреждает."""
    repo = _repo(
        tmp_path / "repo", remote="git@gitlab.example.test:group/sub/project.git"
    )
    _project_json(repo, {**_GITLAB_FIELD, "unexpected": "value"})
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    code, checks = _health(repo, bin_dir)

    assert code == 1
    assert checks["files.project_json"]["status"] == "fail"
    project = checks["tracker.project"]
    assert project["status"] == "warn"
    assert project["message"] == (
        "поле tracker не применено: .harness/project.json или поле tracker некорректны "
        "(см. files.project_json); трекер проекта: gitlab, хост gitlab.example.test, "
        "проект group/sub/project (источник: origin)"
    )


def test_project_local_field_with_github_origin_skips_online_checks(
    tmp_path: Path,
) -> None:
    """Проверить, что поле local при GitHub-origin — ok без расхождения и без вызовов gh."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _project_json(repo, {"type": "local"})
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    gh_log = _fake_gh(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.project"]["status"] == "ok"
    assert checks["tracker.project"]["message"] == (
        "трекер проекта: local (источник: поле tracker)"
    )
    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "локальный" in str(checks[check_id]["message"])
    assert _invocations(gh_log) == []


# --- labels ------------------------------------------------------------------------------------------


def test_missing_triage_labels_file_warns(tmp_path: Path) -> None:
    """Проверить, что отсутствие файла triage-labels.md вызывает предупреждение."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "triage-labels.md" in str(labels["message"])


def test_missing_labels_warns_and_list_missing_names(tmp_path: Path) -> None:
    """Проверить, что отсутствующие метки вызывают предупреждение и перечисляются в сообщении."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                json.dumps([{"name": "hitl", "color": "fbca04"}]),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "afk" in str(labels["message"])
    assert "status::ready" in str(labels["message"])
    missing_part = str(labels["message"]).split("отсутствуют:")[1].split(";")[0]
    assert "hitl" not in missing_part
    assert not any(call[:2] == ["label", "create"] for call in _invocations(gh_log))


def test_missing_labels_with_fix_creates_only_missing_ones(tmp_path: Path) -> None:
    """Проверить, что вызов --fix создает только отсутствующие метки трекера."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                json.dumps([{"name": "hitl", "color": "fbca04"}]),
                "",
            ),
            "label create afk --color #54c1e8 -R github.com/acme/widgets": (0, "", ""),
            "label create status::ready --color #0e8a16 -R github.com/acme/widgets": (
                0,
                "",
                "",
            ),
        },
    )

    _exit_code, _checks, data = _health_full(repo, bin_dir, online=True, fix=True)

    invocations = _invocations(gh_log)
    created = [call for call in invocations if call[:2] == ["label", "create"]]
    created_names = {call[2] for call in created}
    assert created_names == {"afk", "status::ready"}
    assert all("--force" not in call for call in created)
    fixes_applied = data["fixes_applied"]
    assert isinstance(fixes_applied, list) and fixes_applied
    assert any("afk" in entry and "status::ready" in entry for entry in fixes_applied)


@pytest.mark.parametrize(
    ("tracker_field", "remote"),
    [
        (None, "https://gitlab.example.test:4443/group/sub/project.git"),
        (_GITLAB_FIELD, "https://gitlab.example.test:4443/group/other.git"),
    ],
    ids=["origin", "tracker-field-over-origin"],
)
def test_gitlab_labels_fix_addresses_the_project_and_leaves_project_json_untouched(
    tmp_path: Path, tracker_field: dict[str, str] | None, remote: str
) -> None:
    """Проверить, что health --fix создаёт метки GitLab с явным -R https://<host>/<project> из
    резолвера (поле tracker побеждает origin) и --hostname для glab api и не меняет
    .harness/project.json — в том числе не пишет поле tracker."""
    repo = _repo(tmp_path / "repo", remote=remote)
    _project_json(repo, tracker_field)
    project_json = repo / ".harness" / "project.json"
    before = project_json.read_bytes()
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    host = "gitlab.example.test:4443"
    url = f"https://{host}/group/sub/project"
    responses = _gitlab_access(host, "group%2Fsub%2Fproject", 30)
    responses.update(
        {
            f"api --hostname {host} --paginate projects/group%2Fsub%2Fproject/labels": (
                0,
                json.dumps([{"name": "hitl", "color": "#fbca04"}]),
                "",
            ),
            f"label create --name afk --color #54c1e8 -R {url}": (0, "", ""),
            f"label create --name status::ready --color #0e8a16 -R {url}": (0, "", ""),
        }
    )
    glab_log = _fake_glab(bin_dir, responses, host=host)

    _exit_code, _checks, data = _health_full(repo, bin_dir, online=True, fix=True)

    created = [
        call for call in _invocations(glab_log) if call[:2] == ["label", "create"]
    ]
    assert created == [
        ["label", "create", "--name", "afk", "--color", "#54c1e8", "-R", url],
        ["label", "create", "--name", "status::ready", "--color", "#0e8a16", "-R", url],
    ]
    fixes_applied = data["fixes_applied"]
    assert isinstance(fixes_applied, list) and any("afk" in e for e in fixes_applied)
    assert project_json.read_bytes() == before


def test_color_mismatch_warns_and_never_recolors_even_with_fix(tmp_path: Path) -> None:
    """Проверить, что несовпадение цвета метки выдает предупреждение и не перезаписывает цвет даже с --fix."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(
                    [
                        {"name": "hitl", "color": "000000"},
                        {"name": "afk", "color": "54c1e8"},
                        {"name": "status::ready", "color": "0e8a16"},
                    ]
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True, fix=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "hitl" in str(labels["message"])
    invocations = _invocations(gh_log)
    assert not any(
        call[:2] in (["label", "create"], ["label", "edit"]) for call in invocations
    )


def test_labels_beyond_gh_default_page_size_are_not_reported_missing(
    tmp_path: Path,
) -> None:
    """Проверить, что метки за пределами первой страницы пагинации не считаются отсутствующими."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    # 32 filler labels ahead of the 3 canonical ones, well past gh's own default page of 30.
    filler = [{"name": f"filler-{i:03d}", "color": "cccccc"} for i in range(32)]
    canonical = [
        {"name": "hitl", "color": "fbca04"},
        {"name": "afk", "color": "54c1e8"},
        {"name": "status::ready", "color": "0e8a16"},
    ]
    gh_log = _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(filler + canonical),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True, fix=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "ok"
    invocations = _invocations(gh_log)
    assert not any(call[:2] == ["label", "create"] for call in invocations)


def test_all_labels_present_and_matching_is_ok(tmp_path: Path) -> None:
    """Проверить, что при наличии всех совпадающих меток возвращается статус ok."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api --hostname github.com repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --hostname github.com --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(
                    [
                        {"name": "hitl", "color": "fbca04"},
                        {"name": "afk", "color": "54c1e8"},
                        {"name": "status::ready", "color": "0e8a16"},
                    ]
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.labels"]["status"] == "ok"


# --- reachability ------------------------------------------------------------------------------------


def test_unreachable_origin_fails_reachability(tmp_path: Path) -> None:
    """Проверить, что недоступный origin приводит к ошибке проверки доступности."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (128, "", "could not resolve host")})
    _fake_gh(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.reachability"]["status"] == "fail"
