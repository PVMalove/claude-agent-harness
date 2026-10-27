"""Read-only maintenance facts. Deletion policy belongs to harness.cleanup."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from harness.cleanup import CleanupPlan, WorktreeFact, plan_cleanup, worktree_facts


@dataclass
class Size:
    bytes: int = 0
    files: int = 0


def path_size(path: Path) -> Size:
    """Count regular files without following symlinks or hiding inaccessible paths."""
    result = Size()

    def count(candidate: Path) -> None:
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode):
            result.bytes += info.st_size
            result.files += 1

    if path.is_symlink():
        return result
    if path.is_dir():

        def fail(error: OSError) -> None:
            raise error

        for parent, _, files in os.walk(path, followlinks=False, onerror=fail):
            for name in files:
                count(Path(parent) / name)
    else:
        count(path)
    return result


@dataclass
class CleanupSummary:
    plan: CleanupPlan
    categories: dict[str, Size] = field(default_factory=dict)

    @property
    def total(self) -> Size:
        return Size(
            sum(row.bytes for row in self.categories.values()),
            sum(row.files for row in self.categories.values()),
        )


def summarize(plan: CleanupPlan) -> CleanupSummary:
    summary = CleanupSummary(plan)
    root = Path(plan["root"])
    for item in plan["remove"]:
        if item["kind"] not in ("directory", "file", "worktree"):
            continue
        path = Path(item["path"])
        parts = path.relative_to(root).parts
        if parts[0] == ".sandboxes":
            parts = parts[1:]
        category = (
            "/".join(parts[:2])
            if parts[0] == "runs" and len(parts) > 1 and parts[1] in ("tests", "qa")
            else parts[0]
        )
        size = path_size(path)
        row = summary.categories.setdefault(category, Size())
        row.bytes += size.bytes
        row.files += size.files
    return summary


@dataclass
class MaintenanceView:
    soft: CleanupSummary
    hard: CleanupSummary
    worktrees: list[WorktreeFact]
    worktree_error: str = ""
    tree_sizes: dict[Path, Size] = field(default_factory=dict)


def load_maintenance(repo: Path) -> MaintenanceView:
    soft = summarize(plan_cleanup(repo, "soft"))
    hard = summarize(plan_cleanup(repo, "hard"))
    try:
        trees = worktree_facts(repo)
    except ValueError as exc:
        return MaintenanceView(soft, hard, [], str(exc))
    view = MaintenanceView(soft, hard, trees)
    for tree in trees:
        if tree.reason != "current checkout" and tree.path.is_relative_to(
            Path(hard.plan["root"])
        ):
            try:
                view.tree_sizes[tree.path] = path_size(tree.path)
            except OSError:
                pass  # Unknown size stays unavailable, never becomes zero.
    return view


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def summary_text(summary: CleanupSummary, language: str) -> str:
    total = summary.total
    lines = [
        f"{format_size(total.bytes)} · {total.files} "
        + ("файлов" if language == "ru" else "files"),
        *(
            f"{name}: {format_size(size.bytes)} · {size.files}"
            for name, size in summary.categories.items()
        ),
        ("Пропущено" if language == "ru" else "Skipped")
        + f": {len(summary.plan['skipped'])}",
    ]
    lines.extend(
        f"{item['kind']}: {item['path']}"
        for item in summary.plan["remove"]
        if item["kind"] in ("worktree", "container", "network")
    )
    for item in summary.plan["skipped"]:
        lines.append(f"○ {item['path']}: {reason_text(item['reason'], language)}")
    if not summary.plan["remove"]:
        lines.insert(
            0, "Временных файлов нет" if language == "ru" else "No temporary files"
        )
    return "\n".join(lines)


_REASONS_RU = {
    "current checkout": "текущий checkout",
    "not a direct managed worktree": "вне управляемого каталога worktree",
    "Git or ledger state unavailable": "Git или ledger недоступен",
    "referenced by an active batch": "используется активным batch",
    "unowned local branch": "ветка вне шаблона проекта",
    "worktree newer than minimum age": "worktree моложе 24 ч",
    "worktree status unavailable": "статус Git недоступен",
    "worktree has uncommitted changes": "есть незакоммиченные изменения",
    "local commits are not preserved on origin": "коммиты не сохранены на origin",
    "active or newer than minimum age": "активные данные или моложе 24 ч",
    "registered QA worktree or Git unavailable": "зарегистрированный QA worktree или Git недоступен",
    "not a local directory": "каталог вне локального хранилища",
    "Git unavailable": "Git недоступен",
    "worktree is not registered": "worktree не зарегистрирован",
}


def reason_text(reason: str, language: str) -> str:
    if not reason:
        return "Можно удалить" if language == "ru" else "Can remove"
    return _REASONS_RU.get(reason, reason) if language == "ru" else reason
