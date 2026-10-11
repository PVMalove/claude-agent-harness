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

from .catalog import CatalogEntry, Reversibility


@dataclass(frozen=True)
class ProjectTarget:
    repo: Path
    action: Literal["init", "adopt", "update"]
    capabilities: tuple[str, ...]
    conflicts: tuple[str, ...]


def inspect_target(value: str) -> ProjectTarget:
    if not value.strip():
        raise ValueError("Выберите каталог проекта")
    repo = Path(value).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError("Каталог проекта не существует")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Git недоступен или не отвечает") from None
    if result.returncode != 0:
        raise ValueError(
            "Это не Git-проект. Сначала выполните git init в выбранном каталоге"
        )
    repo = Path(result.stdout.strip()).resolve()
    from harness.bin.harness import load_lock

    selected: tuple[str, ...] = ()
    try:
        lock = load_lock(repo)
        if lock is not None:
            capabilities = lock["capabilities"]
            known = dict(capability_options())
            if (
                not isinstance(capabilities, list)
                or not all(isinstance(x, str) for x in capabilities)
                or any(name not in known for name in capabilities)
            ):
                raise ValueError
            selected = tuple(capabilities)
    except (SystemExit, ValueError, KeyError, TypeError):
        raise ValueError(
            "harness.lock повреждён; сначала проверьте существующую установку"
        ) from None
    conflicts = tuple(
        str(path.relative_to(repo))
        for path in (
            repo / ".agents/skills",
            repo / ".claude/skills",
            repo / ".harness/skills",
        )
        if path.exists() or path.is_symlink()
    )
    existing = conflicts or any(
        (repo / name).exists() for name in ("AGENTS.md", "CLAUDE.md")
    )
    return ProjectTarget(
        repo,
        "update" if lock is not None else ("adopt" if existing else "init"),
        selected,
        conflicts,
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
    from harness.bin.harness import load_lock, package_files, snapshot_diff
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
    lock = load_lock(repo)
    owned = (lock or {}).get("files", {})
    if not isinstance(owned, dict):
        raise ValueError("Повреждённый список управляемых файлов в harness.lock")
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
        diff = snapshot_diff(repo, [capability])
        blocked.update(diff.get("local_changed", []))
        blocked.update(diff.get("conflict", []))
    return tuple(sorted(blocked))
