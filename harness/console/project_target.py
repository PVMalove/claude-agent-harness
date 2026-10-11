"""Read-only project selection and explicit installation plans."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .configuration import PACKAGE


@dataclass(frozen=True)
class ProjectTarget:
    repo: Path
    action: str
    capabilities: tuple[str, ...]
    conflicts: tuple[str, ...]


def inspect_target(value: str) -> ProjectTarget:
    if not value.strip():
        raise ValueError("Выберите директорию проекта")
    repo = Path(value).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError("Директория проекта не существует")
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
            "Это не Git-проект. Сначала выполните git init в выбранной директории"
        )
    repo = Path(result.stdout.strip()).resolve()
    lock = repo / ".harness/harness.lock"
    selected: tuple[str, ...] = ()
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            capabilities = data["capabilities"]
            if not isinstance(capabilities, list) or not all(
                isinstance(x, str) for x in capabilities
            ):
                raise ValueError
            selected = tuple(capabilities)
            if any(name not in dict(capability_options()) for name in selected):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
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
        "update" if lock.exists() else ("adopt" if existing else "init"),
        selected,
        conflicts,
    )


def capability_options() -> tuple[tuple[str, str], ...]:
    catalog = json.loads((PACKAGE / "CAPABILITIES.json").read_text(encoding="utf-8"))
    labels = {
        "project-foundation": "Основа — небольшой набор инструкций и навыков",
        "mattpocock-suite": "Upstream — полный закреплённый набор навыков",
        "pvmalove-suite": "Workflow — задачи, QA и ручная настройка project.json",
        "backend-orchestration": "Backend — workflow и роли; добавляет orchestration.json",
    }
    return tuple((name, labels[name]) for name in labels if name in catalog)


def installation_plan(target: ProjectTarget, capability: str) -> list[str]:
    if capability not in dict(capability_options()):
        raise ValueError("Неизвестная capability")
    return ["harness", target.action, str(target.repo), "--capability", capability]


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
