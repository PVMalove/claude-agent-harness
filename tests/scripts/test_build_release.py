"""Release artifacts must contain the tracked installation payload only."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.build_release import build_release, check_release, release_notes


def test_release_archive_excludes_repository_docs_and_checks_version(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    files = {
        "harness/VERSION": "1.0.0\n",
        "harness/bin/harness.py": "# installed CLI\n",
        "skills/example/SKILL.md": "# installed skill\n",
        "README.md": "# Agent Harness\n",
        "LICENSE": "MIT\n",
        "third_party/example/LICENSE": "upstream license\n",
        "docs/README.md": "# Source only\n",
        "docs/LICENSE": "source-only license\n",
        "tests/test_example.py": "pass\n",
        ".github/workflows/verify.yml": "name: verify\n",
        "CHANGELOG.md": "## [1.0.0] - 2026-09-28\n\n### Added\n\n- First release.\n\n### Fixed\n\n- None.\n\n### Breaking Changes\n\n- None.\n",
    }
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    archive, checksum, notes = build_release(repo, "v1.0.0", tmp_path / "out")
    with tarfile.open(archive, "r:gz") as bundle:
        assert set(bundle.getnames()) == {
            "harness/VERSION",
            "harness/bin/harness.py",
            "skills/example/SKILL.md",
            "README.md",
            "LICENSE",
            "third_party/example/LICENSE",
        }
    assert checksum.read_text(encoding="utf-8").endswith(f"  {archive.name}\n")
    assert "First release." in notes.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        build_release(repo, "v1.0.1", tmp_path / "wrong")


def test_release_notes_require_version_section() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        release_notes("## [0.9.0]\n\n### Added\n", "1.0.0")


def test_release_notes_list_only_the_categories_a_release_has() -> None:
    """Keep a Changelog: no placeholder categories; a present category needs an entry."""
    notes = release_notes("## [1.0.0]\n\n### Added\n\n- First.\n", "1.0.0")
    assert notes == "### Added\n\n- First.\n"
    for changelog, message in (
        ("## [1.0.0]\n\n### Added\n\n- First.\n\n### Fixed\n", "empty Fixed"),
        ("## [1.0.0]\n\n### Misc\n\n- Something.\n", "unknown category: Misc"),
        ("## [1.0.0]\n\n- Loose entry.\n", "no ### category"),
        ("## [1.0.0]\n", "no release notes"),
    ):
        with pytest.raises(ValueError, match=message):
            release_notes(changelog, "1.0.0")


def test_check_release_validates_without_building(tmp_path: Path) -> None:
    """The release workflow's preflight: a wrong tag, version or CHANGELOG fails before verify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for relative, content in {
        "harness/VERSION": "1.0.0\n",
        "README.md": "# Agent Harness\n",
        "CHANGELOG.md": "## [1.0.0]\n\n### Added\n\n- First.\n\n### Fixed\n\n- None.\n\n"
        "### Breaking Changes\n\n- None.\n",
    }.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    notes, files = check_release(repo, "v1.0.0")

    assert "First." in notes
    assert files == ["README.md", "harness/VERSION"]
    assert not (repo / "out").exists()
    for tag, message in (
        ("v1.0", "SemVer"),
        ("v1.0.0-rc1", "SemVer"),
        ("v1.0.1", "does not match"),
    ):
        with pytest.raises(ValueError, match=message):
            check_release(repo, tag)
