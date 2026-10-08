"""Локальные пути проекта для временных данных harness.

Связанные Git worktree используют общее хранилище `.harness` основного checkout.
Вызывающая сторона решает, создавать ли директорию; само разрешение путей доступно только для чтения.
"""

from __future__ import annotations

import subprocess
import os
from pathlib import Path

SANDBOXES_DIR = ".sandboxes"
SANDBOX_CATEGORIES = frozenset(
    {"cache", "logs", "scratch", "pr_body", "runs", "reports", "worktrees"}
)
LEGACY_STORAGE_DIRS = (".cache", "test-logs", "tmp", "reports", "scratch")
# Bound of each local `git rev-parse`/`git config` probe; no answer counts as an unknown checkout.
GIT_TIMEOUT_SECONDS = 30


def _unresolved_storage(checkout: Path, *, required: bool) -> Path:
    """Preserve legacy local fallback unless the caller requires a known main checkout."""
    if required:
        raise ValueError("cannot locate main checkout for shared storage")
    return checkout / ".harness"


def _git(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    """Run git trusting `checkout` (safe.directory); None when git cannot start or does not answer."""
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={checkout}", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_main_checkout(checkout: Path, common: Path) -> bool:
    """A configured owner must be the main Git directory's actual, existing checkout."""
    if not checkout.is_dir():
        return False
    result = _git(
        checkout, "-C", str(checkout), "rev-parse", "--show-toplevel", "--git-dir"
    )
    if result is None:
        return False
    lines = result.stdout.splitlines()
    if (
        result.returncode != 0
        or len(lines) != 2
        or Path(lines[0]).resolve() != checkout
    ):
        return False
    git_dir = Path(lines[1])
    if not git_dir.is_absolute():
        git_dir = checkout / git_dir
    return git_dir.resolve() == common


def storage_root(repo: Path, *, require_main_checkout: bool = False) -> Path:
    """Найти общий `.harness` для корня репозитория и связанных worktree."""
    checkout = repo.expanduser().resolve()
    unresolved_pointer = require_main_checkout and (checkout / ".git").is_file()
    result = _git(
        checkout,
        "-C",
        str(checkout),
        "rev-parse",
        "--show-toplevel",
        "--git-common-dir",
    )
    # No runnable Git: a linked worktree must not become a separate cache owner.
    if result is None or result.returncode != 0:
        return _unresolved_storage(checkout, required=unresolved_pointer)
    lines = result.stdout.splitlines()
    if len(lines) != 2 or Path(lines[0]).resolve() != checkout:
        return _unresolved_storage(checkout, required=require_main_checkout)
    common = Path(lines[1])
    if not common.is_absolute():
        common = checkout / common
    common = common.resolve()
    if not common.is_dir():
        return _unresolved_storage(checkout, required=require_main_checkout)
    # --separate-git-dir has no backlink to its checkout. Use an explicit shared
    # core.worktree when configured, rather than treating metadata as source files.
    configured = _git(
        checkout,
        "--git-dir",
        str(common),
        "config",
        "--local",
        "--get",
        "core.worktree",
    )
    if configured is None:
        return _unresolved_storage(checkout, required=require_main_checkout)
    if configured.returncode == 0:
        value = configured.stdout.removesuffix("\n")
        if not value:
            return _unresolved_storage(checkout, required=require_main_checkout)
        main = Path(value)
        if not main.is_absolute():
            main = common / main
        main = main.resolve()
        if not _is_main_checkout(main, common):
            return _unresolved_storage(checkout, required=require_main_checkout)
        return main / ".harness"
    if configured.returncode != 1 or common.name != ".git":
        return _unresolved_storage(checkout, required=require_main_checkout)
    return common.parent / ".harness"


def sandboxes_root(repo: Path) -> Path:
    """Вернуть путь к общим временным данным основного checkout и связанных worktree.

    Функция только строит путь; доступность и безопасность проверяются при использовании.
    """
    return storage_root(repo) / SANDBOXES_DIR


def storage_path(repo: Path, *parts: str) -> Path:
    """Построить путь внутри известной категории общего хранилища.

    Компоненты должны быть одиночными именами; неизвестные категории, выход через
    symlink и переход за границу `.harness/.sandboxes` вызывают ``ValueError``.
    """
    if not parts or any(
        not part
        or part in {".", ".."}
        or Path(part).name != part
        or "/" in part
        or "\\" in part
        for part in parts
    ):
        raise ValueError("storage path components must be single nonempty names")
    category = parts[0]
    if category in SANDBOX_CATEGORIES:
        root = sandboxes_root(repo)
        root_name = "sandboxes root"
    else:
        raise ValueError(
            f"unknown storage category {category!r}, expected one of {sorted(SANDBOX_CATEGORIES)}"
        )
    if root.parent.is_symlink() or root.is_symlink():
        raise ValueError("storage root must not be a symlink")
    target = root.joinpath(*parts)
    resolved_target, resolved_root = target.resolve(), root.resolve()
    if os.name == "nt":
        resolved_target = _windows_comparable(resolved_target)
        resolved_root = _windows_comparable(resolved_root)
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"path escaped {root_name}: {target}")
    return target


def _windows_comparable(path: Path) -> Path:
    """Сравнивать пути Windows без учёта регистра и без префикса `\\\\?\\`.

    `Path.resolve()` оставляет этот префикс, если файл заменяется во время разрешения пути,
    как при параллельной публикации индекса памяти через `os.replace`.
    """
    return Path(os.path.normcase(str(path)).removeprefix("\\\\?\\"))


def sandboxes_health(repo: Path) -> list[str]:
    """Вернуть предупреждения о недоступном хранилище и старых каталогах.

    Проверка не создаёт файлов и не следует за symlink на корень хранилища.
    """
    lines: list[str] = []
    storage = storage_root(repo)
    sandboxes = sandboxes_root(repo)

    if storage.is_symlink() or sandboxes.is_symlink():
        return [
            "ПРЕДУПРЕЖДЕНИЕ: корень .harness/.sandboxes не должен быть symlink",
            "КАК ИСПРАВИТЬ: замените symlink локальной директорией",
        ]

    try:
        rel_sandboxes = sandboxes.relative_to(repo).as_posix()
    except ValueError:
        rel_sandboxes = str(sandboxes)

    if sandboxes.exists():
        if not sandboxes.is_dir():
            lines.append(
                f"ПРЕДУПРЕЖДЕНИЕ: {rel_sandboxes} существует, но не является директорией"
            )
            lines.append(
                f"КАК ИСПРАВИТЬ: удалите {rel_sandboxes} и создайте директорию"
            )
        elif not (os.access(sandboxes, os.R_OK) and os.access(sandboxes, os.W_OK)):
            lines.append(
                f"ПРЕДУПРЕЖДЕНИЕ: отсутствует доступ на чтение/запись в {rel_sandboxes}"
            )
            lines.append(
                f"КАК ИСПРАВИТЬ: проверьте права доступа к директории {rel_sandboxes}"
            )
    else:
        parent = sandboxes.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            try:
                rel_parent = parent.relative_to(repo).as_posix()
            except ValueError:
                rel_parent = str(parent)
            lines.append(
                f"ПРЕДУПРЕЖДЕНИЕ: невозможно создать {rel_sandboxes} в {rel_parent} (нет прав на запись)"
            )
            lines.append(f"КАК ИСПРАВИТЬ: проверьте права доступа к {rel_parent}")

    legacy_found = [name for name in LEGACY_STORAGE_DIRS if (storage / name).exists()]
    if legacy_found:
        lines.append(
            f"ПРЕДУПРЕЖДЕНИЕ: обнаружены устаревшие директории вне .sandboxes: {', '.join(sorted(legacy_found))}"
        )
        lines.append(
            "КАК ИСПРАВИТЬ: выполните harness cleanup для очистки устаревших данных"
        )

    return lines


def validate_sandboxes(repo: Path, problems: list[str]) -> None:
    """Добавить фатальные ошибки корня хранилища в список ``problems``.

    Отклоняет symlink и недоступный каталог, чтобы запись не вышла за пределы проекта.
    """
    sandboxes = sandboxes_root(repo)
    if sandboxes.parent.is_symlink() or sandboxes.is_symlink():
        problems.append(f"{sandboxes} storage root must not be a symlink")
        return
    if sandboxes.exists():
        if not sandboxes.is_dir():
            problems.append(f"{sandboxes} exists but is not a directory")
        elif not (os.access(sandboxes, os.R_OK) and os.access(sandboxes, os.W_OK)):
            problems.append(f"no read/write access to {sandboxes}")
    else:
        parent = sandboxes.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            problems.append(f"cannot create {sandboxes} in {parent}: permission denied")
