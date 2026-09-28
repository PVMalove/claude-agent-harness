"""The console mark and banner: stdlib data, testable without textual."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.console import brand
from harness.console.help_text import HELP_MARKDOWN


def test_logo_rows_are_one_width_and_the_banner_has_a_line_per_row() -> None:
    assert len({len(row) for row in brand.LOGO}) == 1
    info = brand.BannerInfo(
        version="9.9.9", capabilities=(), repo="~/repo", branch=None
    )
    assert len(brand.banner_lines(info)) == len(brand.LOGO)


def test_banner_names_version_capabilities_path_and_branch() -> None:
    info = brand.BannerInfo(
        version="1.0.0",
        capabilities=("pvmalove-suite", "backend-orchestration"),
        repo="~/work/app",
        branch="feature/issue-1-x",
    )
    lines = brand.banner_lines(info)
    assert f"{brand.PRODUCT} 1.0.0" in lines
    assert "pvmalove-suite · backend-orchestration" in lines
    assert "~/work/app" in lines
    assert "ветка feature/issue-1-x" in lines


def test_banner_explains_a_missing_harness_and_branch() -> None:
    info = brand.BannerInfo(
        version="1.0.0", capabilities=(), repo="/srv/app", branch=None
    )
    lines = brand.banner_lines(info)
    assert "харнесс не установлен" in lines
    assert "ветка не определена" in lines


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
    lock.write_text(json.dumps({"capabilities": ["pvmalove-suite"]}), encoding="utf-8")
    assert brand.installed_capabilities(tmp_path) == ("pvmalove-suite",)


def test_current_branch_reads_git_and_is_none_outside_a_repository(
    tmp_path: Path,
) -> None:
    assert brand.current_branch(tmp_path) is None
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True)
    assert brand.current_branch(tmp_path) == "trunk"


def test_help_describes_every_menu_section_and_the_exit_key() -> None:
    for section in (
        "Diagnostics",
        "Harness",
        "Orchestration",
        "Reports",
        "Repo Map",
        "Help",
    ):
        assert f"| {section} |" in HELP_MARKDOWN
    assert "`Ctrl+Q`" in HELP_MARKDOWN and "`F1`" in HELP_MARKDOWN
