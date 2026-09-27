"""Project-local paths for disposable harness data.

Linked Git worktrees share their main checkout's `.harness` storage. The caller
decides whether to create a directory; path resolution itself is read-only.
"""

from __future__ import annotations

import subprocess
import os
from pathlib import Path

SANDBOXES_DIR = ".sandboxes"
SANDBOX_CATEGORIES = frozenset(
    {"cache", "logs", "scratch", "runs", "reports", "worktrees"}
)
LEGACY_CATEGORIES = frozenset({".cache", "tmp"})
LEGACY_STORAGE_DIRS = (".cache", "test-logs", "tmp", "reports")


def storage_root(repo: Path) -> Path:
    """Найти общий `.harness` для корня репозитория и связанных worktree."""
    checkout = repo.expanduser().resolve()
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={checkout}",
            "-C",
            str(checkout),
            "rev-parse",
            "--show-toplevel",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return checkout / ".harness"
    lines = result.stdout.splitlines()
    if len(lines) != 2 or Path(lines[0]).resolve() != checkout:
        return checkout / ".harness"
    common = Path(lines[1])
    if not common.is_absolute():
        common = checkout / common
    common = common.resolve()
    if common.name != ".git" or not common.is_dir():
        return checkout / ".harness"
    return common.parent / ".harness"


def sandboxes_root(repo: Path) -> Path:
    """Return the shared .sandboxes root inside the repository's .harness directory."""
    return storage_root(repo) / SANDBOXES_DIR


def storage_path(repo: Path, *parts: str) -> Path:
    """Join known internal categories without allowing callers to escape storage."""
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
    elif category in LEGACY_CATEGORIES:
        root = storage_root(repo)
        root_name = "storage root"
    else:
        raise ValueError(
            f"unknown storage category {category!r}, expected one of {sorted(SANDBOX_CATEGORIES)}"
        )
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escaped {root_name}: {target}")
    return target


def sandboxes_health(repo: Path) -> list[str]:
    """Диагностика готовности .sandboxes/ и предупреждение об устаревших директориях."""
    lines: list[str] = []
    storage = storage_root(repo)
    sandboxes = sandboxes_root(repo)

    try:
        rel_sandboxes = sandboxes.relative_to(repo).as_posix()
    except ValueError:
        rel_sandboxes = str(sandboxes)

    if sandboxes.exists():
        if not sandboxes.is_dir():
            lines.append(f"ПРЕДУПРЕЖДЕНИЕ: {rel_sandboxes} существует, но не является директорией")
            lines.append(f"КАК ИСПРАВИТЬ: удалите {rel_sandboxes} и создайте директорию")
        elif not (os.access(sandboxes, os.R_OK) and os.access(sandboxes, os.W_OK)):
            lines.append(f"ПРЕДУПРЕЖДЕНИЕ: отсутствует доступ на чтение/запись в {rel_sandboxes}")
            lines.append(f"КАК ИСПРАВИТЬ: проверьте права доступа к директории {rel_sandboxes}")
    else:
        parent = sandboxes.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            try:
                rel_parent = parent.relative_to(repo).as_posix()
            except ValueError:
                rel_parent = str(parent)
            lines.append(f"ПРЕДУПРЕЖДЕНИЕ: невозможно создать {rel_sandboxes} в {rel_parent} (нет прав на запись)")
            lines.append(f"КАК ИСПРАВИТЬ: проверьте права доступа к {rel_parent}")

    legacy_found = [
        name for name in LEGACY_STORAGE_DIRS
        if (storage / name).exists()
    ]
    if legacy_found:
        lines.append(
            f"ПРЕДУПРЕЖДЕНИЕ: обнаружены устаревшие директории вне .sandboxes: {', '.join(sorted(legacy_found))}"
        )
        lines.append(
            "КАК ИСПРАВИТЬ: выполните harness cleanup для очистки устаревших данных"
        )

    return lines


def validate_sandboxes(repo: Path, problems: list[str]) -> None:
    """Проверить доступность структуры .sandboxes/ и зафиксировать фатальные ошибки."""
    sandboxes = sandboxes_root(repo)
    if sandboxes.exists():
        if not sandboxes.is_dir():
            problems.append(f"{sandboxes} exists but is not a directory")
        elif not (os.access(sandboxes, os.R_OK) and os.access(sandboxes, os.W_OK)):
            problems.append(f"no read/write access to {sandboxes}")
    else:
        parent = sandboxes.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            problems.append(f"cannot create {sandboxes} in {parent}: permission denied")

