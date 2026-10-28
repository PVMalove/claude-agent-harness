"""The console mark and banner: stdlib data, testable without textual."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from harness.console import brand
from harness.console.help_text import HELP_MARKDOWN, HELP_MARKDOWN_EN


def test_logo_rows_are_one_width_and_the_banner_has_a_line_per_row() -> None:
    assert len({len(row) for row in brand.LOGO}) == 1
    info = brand.BannerInfo(
        version="9.9.9", capabilities=(), repo="~/repo", branch=None
    )
    assert len(brand.banner_lines(info)) == len(brand.LOGO)


def test_banner_names_version_capabilities_path_and_branch() -> None:
    info = brand.BannerInfo(
        version="1.0.0",
        capabilities=("engineering-suite", "backend-orchestration"),
        repo="~/work/app",
        branch="feature/issue-1-x",
        console_commit="f4ad855d",
        installed_version="0.9.0",
        installed_commit="7ac099a2",
    )
    lines = brand.banner_lines(info)
    assert f"{brand.PRODUCT} 1.0.0 · commit f4ad855d" in lines
    assert "Installed Harness 0.9.0 · commit 7ac099a2" in lines[2]
    assert "engineering-suite · backend-orchestration" in lines[2]
    assert "~/work/app" in lines
    assert "ветка feature/issue-1-x · commit не определён" in lines


def test_banner_explains_a_missing_harness_and_branch() -> None:
    info = brand.BannerInfo(
        version="1.0.0", capabilities=(), repo="/srv/app", branch=None
    )
    lines = brand.banner_lines(info)
    assert "харнесс не установлен" in lines
    assert "ветка не определена · commit не определён" in lines
    assert "commit не определён" in lines[1]


def test_display_path_shortens_home(tmp_path: Path) -> None:
    repo = tmp_path / "projects" / "app"
    repo.mkdir(parents=True)
    assert brand.display_path(repo, home=tmp_path) == "~/projects/app"
    assert brand.display_path(repo, home=tmp_path / "elsewhere") == repo.as_posix()


def test_capabilities_come_from_the_lock_and_tolerate_a_broken_one(
    tmp_path: Path,
) -> None:
    assert brand.installed_capabilities(tmp_path) == ()
    lock = tmp_path / ".harness" / "harness.lock"
    lock.parent.mkdir()
    lock.write_text("{not json", encoding="utf-8")
    assert brand.installed_capabilities(tmp_path) == ()
    lock.write_text(json.dumps({"capabilities": ["engineering-suite"]}), encoding="utf-8")
    assert brand.installed_capabilities(tmp_path) == ("engineering-suite",)


def test_current_branch_reads_git_and_is_none_outside_a_repository(
    tmp_path: Path,
) -> None:
    assert brand.current_branch(tmp_path) is None
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True)
    assert brand.current_branch(tmp_path) == "trunk"


def test_banner_reads_installed_revision_from_lock_for_selected_project(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".harness" / "harness.lock"
    lock.parent.mkdir()
    lock.write_text(
        json.dumps(
            {
                "package_version": "1.3.2",
                "source_revision": "7ac099a252d20743a8af4c4af52f48f40381e5e9",
                "capabilities": ["backend-orchestration"],
            }
        ),
        encoding="utf-8",
    )
    info = brand.collect_banner(tmp_path, "1.4.0")
    assert info.version == "1.4.0"
    assert info.installed_version == "1.3.2"
    assert info.installed_commit == "7ac099a2"
    other = tmp_path / "other-project"
    other.mkdir()
    assert brand.collect_banner(other, "1.4.0").installed_commit is None


@pytest.mark.parametrize("revision", [None, "uncommitted", "not a commit", 42])
def test_banner_does_not_invent_a_revision_for_legacy_or_invalid_lock(
    tmp_path: Path, revision: object
) -> None:
    lock = tmp_path / ".harness" / "harness.lock"
    lock.parent.mkdir()
    lock.write_text(
        json.dumps({"package_version": "1.3.2", "source_revision": revision}),
        encoding="utf-8",
    )
    info = brand.collect_banner(tmp_path, "1.4.0")
    assert info.installed_commit is None
    assert (
        "Installed Harness 1.3.2 · commit не определён" in brand.banner_lines(info)[2]
    )
    lock.write_text("{broken", encoding="utf-8")
    assert brand.collect_banner(tmp_path, "1.4.0").installed_commit is None


def test_console_revision_comes_from_its_source_checkout(tmp_path: Path) -> None:
    assert brand.source_commit(tmp_path) is None
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    assert brand.source_commit(source) is None
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "test: source revision",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert brand.source_commit(source) == head[:8]
    subprocess.run(["git", "-C", str(source), "checkout", "-q", "--detach"], check=True)
    assert brand.source_commit(source) == head[:8]
    nested = source / "archive"
    nested.mkdir()
    assert brand.source_commit(nested) is None


@pytest.mark.parametrize(
    ("guide", "sections"),
    [
        (
            HELP_MARKDOWN,
            (
                "Главная",
                "Открыть проект",
                "Обновить Harness",
                "Диагностика",
                "Harness",
                "Оркестрация",
                "Отчёты",
                "Repo Map",
                "Настройка",
                "Справка",
            ),
        ),
        (
            HELP_MARKDOWN_EN,
            (
                "Home",
                "Open project",
                "Update Harness",
                "Diagnostics",
                "Harness",
                "Orchestration",
                "Reports",
                "Repo Map",
                "Configuration",
                "Help",
            ),
        ),
    ],
    ids=["ru", "en"],
)
def test_help_describes_all_ten_menu_numbers_with_their_visible_names(
    guide: str, sections: tuple[str, ...]
) -> None:
    for number, section in enumerate(sections):
        assert f"| `{number}` | {section} |" in guide
    assert "`Ctrl+Q`" in guide and "`F1`" in guide
    for symbol in ("✔", "▶", "Ⅱ", "○", "✖", "⚠"):
        assert f"| {symbol} |" in guide


@pytest.mark.parametrize("guide", [HELP_MARKDOWN, HELP_MARKDOWN_EN], ids=["ru", "en"])
def test_help_matches_the_current_interface(guide: str) -> None:
    assert "project.json" in guide
    assert "Open project" in guide or "«Открыть проект»" in guide
    assert "commit" in guide
    assert ".harness/config_versions/" in guide
    assert "config-versions" not in guide


def _git(repo: Path, *args: str) -> str:
    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _committed_repo(path: Path, message: str = "first") -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", message)
    return path, _git(path, "rev-parse", "HEAD")[:8]


def test_project_commit_is_the_short_head_of_the_selected_project(
    tmp_path: Path,
) -> None:
    repo, head = _committed_repo(tmp_path / "app")
    assert brand.project_commit(repo) == head
    info = brand.collect_banner(repo, "1.4.0")
    assert info.project_commit == head
    assert f"· commit {head}" in brand.banner_lines(info)[4]


def test_project_commit_follows_a_detached_head(tmp_path: Path) -> None:
    repo, head = _committed_repo(tmp_path / "app")
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    info = brand.collect_banner(repo, "1.4.0")
    assert info.branch is None and info.project_commit == head
    assert brand.banner_lines(info)[4] == f"detached HEAD · commit {head}"


def test_a_repository_without_a_commit_or_git_says_the_commit_is_unknown(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert brand.project_commit(empty) is None
    assert brand.project_commit(outside) is None
    line = brand.banner_lines(brand.collect_banner(empty, "1.4.0"))[4]
    assert line.endswith("· commit не определён")
    nowhere = brand.banner_lines(brand.collect_banner(outside, "1.4.0"))[4]
    assert nowhere == "ветка не определена · commit не определён"


@pytest.mark.parametrize(
    "error", [OSError("no git"), subprocess.TimeoutExpired("git", 5)]
)
def test_an_unavailable_git_never_breaks_the_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(subprocess, "run", broken)
    assert brand.project_commit(tmp_path) is None
    assert brand.collect_banner(tmp_path, "1.4.0").project_commit is None


def test_screens_take_colours_from_the_theme_not_from_hex_literals() -> None:
    console = Path(brand.__file__).parent
    allowed = {"brand.py"}
    offenders = [
        f"{path.relative_to(console)}: {match}"
        for path in console.rglob("*.py")
        if path.name not in allowed
        for match in re.findall(
            r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})\b", path.read_text(encoding="utf-8")
        )
    ]
    assert offenders == []


def test_shell_tokens_do_not_shadow_the_palette() -> None:
    assert not set(brand.SHELL_TOKENS) & set(brand.PALETTE)


def test_shell_css_resolves_only_the_shell_tokens() -> None:
    css = "A { border: solid $border-subtle; color: $text; background: $selection-background; }"
    assert brand.shell_css(css) == (
        "A { border: solid #3a3731; color: $text; background: #45332c; }"
    )
