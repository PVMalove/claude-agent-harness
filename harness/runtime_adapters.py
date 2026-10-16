"""Ownership and status of standalone adapters in the managed snapshot."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict

ADAPTER_ROOT = ".harness/adapters"


class AdapterStatus(TypedDict):
    path: str
    status: str
    problem: str | None


def is_adapter(relative: str) -> bool:
    return relative.startswith(ADAPTER_ROOT + "/")


def adapter_problem(
    repo: Path, relative: str, previous_hash: str | None, *, force: bool = False
) -> str | None:
    """Reject unknown and linked paths; explicit force can replace managed regular files."""
    path = repo / relative
    for parent in (repo / ".harness", repo / ADAPTER_ROOT, path):
        if parent.is_symlink():
            return "symlink adapter target"
    if not path.exists():
        return None
    if not path.is_file():
        return "adapter target is not a regular file"
    if previous_hash is None:
        return "unknown adapter target"
    if hashlib.sha256(path.read_bytes()).hexdigest() != previous_hash and not force:
        return "locally changed adapter"
    return None


def adapter_statuses(
    repo: Path, sources: dict[str, bytes], previous: dict[str, str]
) -> list[AdapterStatus]:
    statuses: list[AdapterStatus] = []
    for relative, content in sorted(sources.items()):
        if not is_adapter(relative):
            continue
        path = repo / relative
        problem = adapter_problem(repo, relative, previous.get(relative))
        status = "outdated"
        if not path.exists() and not path.is_symlink():
            status = "missing"
        elif problem is None and path.read_bytes() == content:
            status = "current"
        statuses.append({"path": relative, "status": status, "problem": problem})
    return statuses
