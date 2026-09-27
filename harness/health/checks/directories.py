"""Group 'directories': the `.harness` directories harness commands create lazily (ticket #345).

A missing directory is not a problem as long as it can be created: it is `ok` with "будет создан".
An existing directory the current user cannot write to, a file in its place, or a missing
directory whose nearest existing ancestor is not writable is `fail` - a worker session would crash
on its first write there.

`fix_directory` is the only fix action here: `harness health --fix` creates a missing directory
whose check reported `ok`. It never changes permissions and never deletes anything.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from harness.storage import sandboxes_root, storage_root

from ..context import HealthContext
from ..model import CheckResult, Fix
from .files import BACKEND_ORCHESTRATION_CAPABILITY, _NO_ORCHESTRATION_CAPABILITY_MESSAGE

_GROUP = "directories"

# The sandbox categories in harness/storage.py's SANDBOX_CATEGORIES, in a fixed report order.
SANDBOX_CATEGORY_ORDER: tuple[str, ...] = (
    "cache",
    "logs",
    "scratch",
    "runs",
    "reports",
    "worktrees",
)

# Coordinator ledger state; mirrors harness/orchestration/core/constants.py's STATE_REL, which is
# not importable here (orchestration ships only with backend-orchestration).
ORCHESTRATION_STATE_REL = Path(".harness/orchestration/state")


def _display(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path.parent
    while not current.exists() and not current.is_symlink() and current != current.parent:
        current = current.parent
    return current


def directory_result(check_id: str, repo: Path, path: Path) -> CheckResult:
    """Classify one lazily created harness directory; never creates or changes anything."""
    shown = _display(repo, path)
    if path.is_symlink():
        return CheckResult(
            id=check_id,
            group=_GROUP,
            status="fail",
            message=f"{shown} является symlink; харнесс не пишет через symlink",
            fix=Fix(text=f"замените symlink {shown} локальным каталогом"),
        )
    if path.exists():
        if not path.is_dir():
            return CheckResult(
                id=check_id,
                group=_GROUP,
                status="fail",
                message=f"{shown} существует, но не является каталогом",
                fix=Fix(
                    text=f"удалите или переименуйте файл {shown}, затем выполните harness health --fix"
                ),
            )
        if not _writable(path):
            return CheckResult(
                id=check_id,
                group=_GROUP,
                status="fail",
                message=f"нет прав на запись в каталог {shown}",
                fix=Fix(text=f"выдайте текущему пользователю права на запись в {shown}"),
            )
        return CheckResult(
            id=check_id,
            group=_GROUP,
            status="ok",
            message=f"каталог {shown} доступен на запись",
        )
    ancestor = _nearest_existing_ancestor(path)
    if ancestor.is_dir() and not ancestor.is_symlink() and _writable(ancestor):
        return CheckResult(
            id=check_id,
            group=_GROUP,
            status="ok",
            message=f"каталог {shown} отсутствует, будет создан",
        )
    shown_ancestor = _display(repo, ancestor)
    return CheckResult(
        id=check_id,
        group=_GROUP,
        status="fail",
        message=f"невозможно создать {shown}: нет прав на запись в {shown_ancestor}",
        fix=Fix(text=f"выдайте текущему пользователю права на запись в {shown_ancestor}"),
    )


def fix_directory(repo: Path, path: Path, result: CheckResult) -> str | None:
    """Create `path` when its check said it is missing but creatable; return what was done."""
    if result.status != "ok" or path.exists() or path.is_symlink():
        return None
    path.mkdir(parents=True, exist_ok=True)
    return f"создан каталог {_display(repo, path)}"


# --- Registry-facing checks ------------------------------------------------------------------


def harness_path(context: HealthContext) -> Path:
    return storage_root(context.repo)


def sandboxes_path(context: HealthContext) -> Path:
    return sandboxes_root(context.repo)


def _category_path(category: str) -> Callable[[HealthContext], Path]:
    def resolve(context: HealthContext) -> Path:
        return sandboxes_root(context.repo) / category

    return resolve


def orchestration_state_path(context: HealthContext) -> Path:
    return context.repo / ORCHESTRATION_STATE_REL


# check id -> the directory it covers. registry.py wires both the checks and their fix actions
# from this one table, so a check and its fix can never disagree about the path.
DIRECTORY_PATHS: dict[str, Callable[[HealthContext], Path]] = {
    "directories.harness": harness_path,
    "directories.sandboxes": sandboxes_path,
    **{
        f"directories.{category}": _category_path(category)
        for category in SANDBOX_CATEGORY_ORDER
    },
    "directories.orchestration_state": orchestration_state_path,
}


def _orchestration_enabled(context: HealthContext) -> bool:
    lock = context.lock
    return lock is not None and BACKEND_ORCHESTRATION_CAPABILITY in (
        lock.get("capabilities") or []
    )


def make_check(check_id: str) -> Callable[[HealthContext], CheckResult]:
    resolve = DIRECTORY_PATHS[check_id]

    def check(context: HealthContext) -> CheckResult:
        if check_id == "directories.orchestration_state" and not _orchestration_enabled(context):
            return CheckResult(
                id=check_id,
                group=_GROUP,
                status="skipped",
                message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
            )
        return directory_result(check_id, context.repo, resolve(context))

    check.__name__ = f"check_{check_id.partition('.')[2]}"
    return check


def make_fix(check_id: str) -> Callable[[HealthContext, CheckResult], str | None]:
    resolve = DIRECTORY_PATHS[check_id]

    def fix(context: HealthContext, result: CheckResult) -> str | None:
        return fix_directory(context.repo, resolve(context), result)

    return fix
