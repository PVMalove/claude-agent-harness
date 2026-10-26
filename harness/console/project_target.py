"""Read-only project selection and explicit installation plans."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from harness.configuration_schema import PACKAGE
from harness.health.model import JsonObject

from .catalog import CatalogEntry, Reversibility


@dataclass(frozen=True)
class ProjectTarget:
    repo: Path
    action: Literal["init", "adopt", "update"]
    capabilities: tuple[str, ...]
    conflicts: tuple[str, ...]
    # The project's own files the installation meets: instruction files and discovery paths.
    found: tuple[str, ...] = ()


# A cold, busy machine (a Windows CI runner starting its first git.exe) needs more than a few seconds.
GIT_TIMEOUT_SECONDS = 30


# The status line shows the first line; the hint below it names the next step.
LOCK_BROKEN = (
    "harness.lock повреждён\n"
    "Сначала проверьте существующую установку: «Диагностика» (3)."
)


def _read_lock(repo: Path) -> JsonObject | None:
    """The CLI's lock reader, whose `fail()` exits the process: here a broken lock is an error."""
    from harness.bin.harness import load_lock

    try:
        return load_lock(repo)
    except SystemExit:
        raise ValueError(LOCK_BROKEN) from None


def inspect_target(value: str) -> ProjectTarget:
    if not value.strip():
        raise ValueError("Выберите каталог проекта")
    repo = Path(value).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(
            "Каталог проекта не существует\n"
            "Проверьте путь или выберите папку через «Обзор папок»."
        )
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(
            "Git недоступен или не отвечает\n"
            "Проверьте, что git установлен и доступен в PATH."
        ) from None
    if result.returncode != 0:
        raise ValueError(
            "Это не Git-проект\n"
            "Сначала выполните в каталоге: git init (y — копировать команду)"
        )
    repo = Path(result.stdout.strip()).resolve()
    selected: tuple[str, ...] = ()
    try:
        lock = _read_lock(repo)
        if lock is not None:
            capabilities = lock["capabilities"]
            known = dict(capability_options())
            if (
                not isinstance(lock.get("files", {}), dict)
                or not isinstance(capabilities, list)
                or not all(isinstance(x, str) for x in capabilities)
                or any(name not in known for name in capabilities)
            ):
                raise ValueError
            selected = tuple(capabilities)
    except (ValueError, KeyError, TypeError):
        raise ValueError(LOCK_BROKEN) from None
    conflicts = tuple(
        str(path.relative_to(repo))
        for path in (
            repo / ".agents/skills",
            repo / ".claude/skills",
            repo / ".harness/skills",
        )
        if path.exists() or path.is_symlink()
    )
    found = (
        tuple(name for name in ("AGENTS.md", "CLAUDE.md") if (repo / name).exists())
        + conflicts
    )
    return ProjectTarget(
        repo,
        "update" if lock is not None else ("adopt" if found else "init"),
        selected,
        conflicts,
        found,
    )


_LABELS = {
    "project-foundation": "Основа — небольшой набор инструкций и навыков",
    "mattpocock-suite": "Upstream — полный закреплённый набор навыков",
    "pvmalove-suite": "Workflow — задачи, QA и ручная настройка project.json",
    "backend-orchestration": "Backend — workflow и роли; добавляет orchestration.json",
}


@lru_cache(maxsize=1)
def capability_options() -> tuple[tuple[str, str], ...]:
    """Every catalog capability; a new one without a curated label shows its description."""
    catalog = json.loads((PACKAGE / "CAPABILITIES.json").read_text(encoding="utf-8"))
    return tuple(
        (name, _LABELS.get(name) or str(entry.get("description", name)))
        for name, entry in catalog.items()
    )


def capability_title(name: str) -> str:
    """The short name of a capability (`Workflow`); one without a curated label is its id."""
    head, separator, _ = dict(capability_options()).get(name, name).partition(" — ")
    return head if separator else name


def capability_summary(name: str) -> str:
    """The one-line description of a capability, as a sentence."""
    label = dict(capability_options()).get(name, name)
    _, separator, tail = label.partition(" — ")
    text = tail if separator else label
    return text[:1].upper() + text[1:]


_DETAILS = {
    "project-foundation": "Добавляет инструкции и базовые навыки. Не создаёт project.json и orchestration.json; подходит, когда нужен только общий каркас.",
    "mattpocock-suite": "Полный закреплённый upstream-набор навыков. Не содержит workflow project.json; подходит, когда нужны только навыки.",
    "pvmalove-suite": "Workflow задач, QA и code-review; создаёт project.json. Нужен для мастера project.json; без ролей оркестрации.",
    "backend-orchestration": "Всё из workflow плюс роли и координатор; добавляет orchestration.json. Выбирайте для backend-проекта с автоматизацией этапов.",
}


def capability_details(name: str) -> str:
    """Russian consequences of a capability; unknown ones fall back to the catalog text."""
    return _DETAILS.get(name) or dict(capability_options()).get(name, name)


def installation_entry(target: ProjectTarget, capability: str) -> CatalogEntry:
    """The CLI command of an installation, expressed as an ordinary catalog entry."""
    if capability not in dict(capability_options()):
        raise ValueError("Неизвестная capability")
    return CatalogEntry(
        f"setup-{target.action}",
        "Установить харнесс в выбранный проект",
        ("harness", target.action, "{repo}", "--capability", capability),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"harness/bin/harness.py:cmd_{target.action}",
    )


def installation_plan(target: ProjectTarget, capability: str) -> list[str]:
    return installation_entry(target, capability).cli_argv(target.repo)


def installation_blockers(target: ProjectTarget, capability: str) -> tuple[str, ...]:
    """Use the same snapshot comparison as the CLI, before any installation writes."""
    from harness.bin.harness import package_files, snapshot_diff
    from harness.health.project_files import DISCOVERY_LINKS, native_link_target

    blocked: set[str] = set()
    repo = target.repo
    for relative, expected in DISCOVERY_LINKS.items():
        path = repo / relative
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or os.readlink(path) != native_link_target(
                expected
            ):
                blocked.add(relative)
    lock = _read_lock(repo)
    owned = (lock or {}).get("files", {})
    if not isinstance(owned, dict):
        raise ValueError(LOCK_BROKEN)
    for relative in package_files([capability]):
        path = repo / relative
        if (path.exists() and relative not in owned) or path.is_symlink():
            blocked.add(relative)
        for parent in path.parents:
            if parent == repo:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                blocked.add(str(parent.relative_to(repo)))
    if lock:
        try:
            diff = snapshot_diff(repo, [capability])
        except SystemExit:
            raise ValueError(LOCK_BROKEN) from None
        blocked.update(diff.get("local_changed", []))
        blocked.update(diff.get("conflict", []))
    return tuple(sorted(blocked))
