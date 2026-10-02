"""Storage root selection at repository and nested-directory boundaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness.cleanup import plan_cleanup
from harness.storage import (
    SANDBOX_CATEGORIES,
    SANDBOXES_DIR,
    sandboxes_health,
    sandboxes_root,
    storage_path,
    storage_root,
    validate_sandboxes,
)
from harness.orchestration.core.constants import (
    AGENT_INBOX_REL,
    SANDBOXES_REL,
    SCRATCH_REL,
)


def test_nested_directory_does_not_inherit_parent_repository_cache(
    tmp_path: Path,
) -> None:
    """Проверить, что вложенный каталог не наследует корень хранилища родительского репозитория."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()

    assert storage_root(tmp_path) == tmp_path / ".harness"
    assert storage_root(nested) == nested / ".harness"


def test_storage_root_without_runnable_git_uses_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что при недоступности git используется корень текущего каталога checkout."""

    def missing_git(*_args: object, **_kwargs: object) -> None:
        """Имитировать отсутствие исполняемого файла git."""
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing_git)

    assert storage_root(tmp_path) == tmp_path.resolve() / ".harness"


def test_sandboxes_root_and_categories(tmp_path: Path) -> None:
    """Проверить имя каталога sandboxes, ожидаемый список категорий и путь к корню sandboxes."""
    assert SANDBOXES_DIR == ".sandboxes"
    expected_categories = {
        "cache",
        "logs",
        "scratch",
        "pr_body",
        "runs",
        "reports",
        "worktrees",
    }
    assert set(SANDBOX_CATEGORIES) == expected_categories
    assert sandboxes_root(tmp_path) == tmp_path / ".harness" / ".sandboxes"


def test_sandboxes_root_in_linked_worktree(tmp_path: Path) -> None:
    """Проверить определение корня sandboxes и путей хранения внутри связанного git worktree."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    readme = repo / "README.md"
    readme.write_text("main", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial commit"], check=True
    )

    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True
    )

    assert storage_root(worktree) == repo / ".harness"
    assert sandboxes_root(worktree) == repo / ".harness" / ".sandboxes"
    assert (
        storage_path(worktree, "runs", "test-1")
        == repo / ".harness" / ".sandboxes" / "runs" / "test-1"
    )


def test_storage_path_categories_and_boundary(tmp_path: Path) -> None:
    """Проверить формирование путей хранения для всех категорий sandboxes и нахождение внутри корня."""
    for cat in SANDBOX_CATEGORIES:
        path = storage_path(tmp_path, cat, "sub1", "sub2")
        assert path == tmp_path / ".harness" / ".sandboxes" / cat / "sub1" / "sub2"
        assert path.is_relative_to(tmp_path / ".harness" / ".sandboxes")


def test_storage_path_rejects_legacy_categories(tmp_path: Path) -> None:
    """Проверить отклонение устаревших категорий хранения при вызове storage_path."""
    with pytest.raises(ValueError, match="unknown storage category"):
        storage_path(tmp_path, ".cache", "repo_map", "results")

    with pytest.raises(ValueError, match="unknown storage category"):
        storage_path(tmp_path, "tmp", "tests")


def test_storage_path_escape_and_invalid_components(tmp_path: Path) -> None:
    """Проверить валидацию компонентов пути и отклонение попыток выхода за пределы sandboxes."""
    # Empty calls or invalid names
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path)
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "..")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", ".")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "foo/bar")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "foo\\bar")

    # Unknown category
    with pytest.raises(ValueError, match="unknown storage category"):
        storage_path(tmp_path, "invalid_cat", "sub")


@pytest.mark.skipif(os.name != "nt", reason="Windows path resolution")
def test_storage_path_accepts_extended_prefix_from_racing_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Принять `\\\\?\\`-путь, который `resolve()` оставляет при параллельном `os.replace`."""
    original = Path.resolve

    def racing_resolve(self: Path, strict: bool = False) -> Path:
        resolved = original(self, strict)
        if self.name == "index.sqlite3":
            return Path("\\\\?\\" + str(resolved).upper())
        return resolved

    monkeypatch.setattr(Path, "resolve", racing_resolve)
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    assert (
        path == storage_root(tmp_path) / SANDBOXES_DIR / "cache" / "memory" / path.name
    )


@pytest.mark.parametrize("linked_part", [".harness", ".sandboxes"])
def test_storage_and_cleanup_reject_linked_roots(
    tmp_path: Path, linked_part: str
) -> None:
    """Проверить отклонение символических ссылок в путях .harness и .sandboxes при операциях хранения и очистки."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    harness_dir = repo / ".harness"
    if linked_part == ".sandboxes":
        harness_dir.mkdir()
        link = harness_dir / ".sandboxes"
    else:
        link = harness_dir
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink"):
        storage_path(repo, "cache", "item")
    with pytest.raises(ValueError, match="symlink"):
        plan_cleanup(repo, "soft")
    problems: list[str] = []
    validate_sandboxes(repo, problems)
    assert any("symlink" in problem for problem in problems)


def test_orchestration_constants_sandboxes_paths() -> None:
    """Проверить соответствие относительных путей констант оркестрации структуре .sandboxes."""
    assert SANDBOXES_REL == Path(".harness") / ".sandboxes"
    assert SCRATCH_REL == Path(".harness") / ".sandboxes" / "scratch"
    assert AGENT_INBOX_REL == Path(".harness") / ".sandboxes" / "scratch" / "inbox"


def test_sandboxes_health_clean_repo(tmp_path: Path) -> None:
    """Проверить отсутствие предупреждений health-check для чистого репозитория с корректным .sandboxes."""
    repo = tmp_path / "clean_repo"
    repo.mkdir()
    (repo / ".harness").mkdir()
    (repo / ".harness" / ".sandboxes").mkdir()
    assert sandboxes_health(repo) == []


def test_sandboxes_health_warns_on_legacy_directories(tmp_path: Path) -> None:
    """Проверить предупреждения health-check при обнаружении устаревших директорий вне .sandboxes."""
    repo = tmp_path / "legacy_repo"
    repo.mkdir()
    harness_dir = repo / ".harness"
    harness_dir.mkdir()
    (harness_dir / ".sandboxes").mkdir()
    for legacy_name in (".cache", "test-logs", "tmp", "reports"):
        (harness_dir / legacy_name).mkdir()

    diagnostics = sandboxes_health(repo)
    assert len(diagnostics) == 2
    assert diagnostics[0].startswith(
        "ПРЕДУПРЕЖДЕНИЕ: обнаружены устаревшие директории вне .sandboxes:"
    )
    assert ".cache" in diagnostics[0]
    assert "test-logs" in diagnostics[0]
    assert "tmp" in diagnostics[0]
    assert "reports" in diagnostics[0]
    assert (
        diagnostics[1]
        == "КАК ИСПРАВИТЬ: выполните harness cleanup для очистки устаревших данных"
    )


def test_sandboxes_health_warns_on_invalid_sandboxes(tmp_path: Path) -> None:
    """Проверить предупреждения health-check, если .sandboxes существует, но не является директорией."""
    repo = tmp_path / "invalid_sandboxes_repo"
    repo.mkdir()
    harness_dir = repo / ".harness"
    harness_dir.mkdir()
    (harness_dir / ".sandboxes").write_text("not a directory", encoding="utf-8")

    diagnostics = sandboxes_health(repo)
    assert any(
        "ПРЕДУПРЕЖДЕНИЕ" in d and "не является директорией" in d for d in diagnostics
    )
    assert any("КАК ИСПРАВИТЬ" in d and "удалите" in d for d in diagnostics)

    problems: list[str] = []
    validate_sandboxes(repo, problems)
    assert len(problems) == 1
    assert "exists but is not a directory" in problems[0]


def test_sandboxes_health_in_worktree(tmp_path: Path) -> None:
    """Проверить диагностику состояния sandboxes при запуске из связанного worktree."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    (repo / "README.md").write_text("main", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True
    )

    harness_dir = repo / ".harness"
    harness_dir.mkdir(exist_ok=True)
    (harness_dir / ".sandboxes").mkdir(exist_ok=True)

    assert sandboxes_health(worktree) == []

    (harness_dir / "tmp").mkdir()
    diagnostics = sandboxes_health(worktree)
    assert any("обнаружены устаревшие директории" in d for d in diagnostics)
