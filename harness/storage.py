"""Project-local paths for disposable harness data.

Linked Git worktrees share their main checkout's `.harness` storage. The caller
decides whether to create a directory; path resolution itself is read-only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def storage_root(repo: Path) -> Path:
    """Return the one `.harness` root shared by a repository's Git worktrees."""
    checkout = repo.expanduser().resolve()
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={checkout}",
            "-C",
            str(checkout),
            "rev-parse",
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
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = checkout / common
    common = common.resolve()
    if common.name != ".git" or not common.is_dir():
        return checkout / ".harness"
    return common.parent / ".harness"


def storage_path(repo: Path, *parts: str) -> Path:
    """Join known internal categories without allowing callers to escape storage."""
    if not parts or any(not part or part in {".", ".."} or Path(part).name != part for part in parts):
        raise ValueError("storage path components must be single nonempty names")
    return storage_root(repo).joinpath(*parts)
