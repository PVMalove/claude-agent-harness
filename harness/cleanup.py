"""Preview and remove only known disposable data inside project-local `.harness`."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import NotRequired, TypedDict

from harness.storage import LEGACY_STORAGE_DIRS, sandboxes_root, storage_root

TERMINAL_BATCH_STATES = {"completed", "failed", "abandoned", "not-required"}
LEGACY_TOP_LEVEL_DIRS = LEGACY_STORAGE_DIRS


class CleanupItem(TypedDict):
    kind: str
    path: str
    branch: NotRequired[str]


class CleanupPlan(TypedDict):
    mode: str
    root: str
    min_age_hours: float
    remove: list[CleanupItem]
    skipped: list[dict[str, str]]


class CleanupResult(TypedDict):
    removed: list[str]
    failed: list[dict[str, str]]
    skipped: list[dict[str, str]]


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    checkout = repo.resolve()
    return subprocess.run(
        ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _inside(root: Path, candidate: Path) -> bool:
    if candidate.is_symlink():
        return False
    try:
        return candidate.resolve().is_relative_to(root.resolve()) and candidate.resolve() != root.resolve()
    except OSError:
        return False


def _old_enough(path: Path, hours: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= hours * 3600
    except OSError:
        return False


def _pid_active(pid: int) -> bool:
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True  # An inconclusive probe must preserve the run.
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _run_active(path: Path) -> bool:
    marker = path / ".active.json"
    if not marker.is_file():
        return False
    try:
        pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
        if not isinstance(pid, int) or pid <= 0:
            return True
        return _pid_active(pid)
    except OSError:
        return True
    except (ValueError, KeyError, TypeError):
        return True


def _registered_worktrees(repo: Path) -> dict[Path, str | None] | None:
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    entries: dict[Path, str | None] = {}
    path: Path | None = None
    branch: str | None = None
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
        elif not line and path is not None:
            entries[path] = branch
            path, branch = None, None
    return entries


def _active_worktrees(root: Path) -> set[Path] | None:
    state = root / "orchestration" / "state"
    if not state.exists():
        return set()
    pointer = state / "ledger.json"
    try:
        if pointer.exists():
            generation = json.loads(pointer.read_text(encoding="utf-8"))["generation"]
            if not isinstance(generation, str) or not re.fullmatch(r"generation-[A-Za-z0-9-]+", generation):
                return None
            records = state / "generations" / generation / "batches"
        else:
            records = state / "batches"
        if not records.exists():
            return None if pointer.exists() else set()
        active: set[Path] = set()
        for record in records.glob("batch-*.json"):
            batch = json.loads(record.read_text(encoding="utf-8"))
            if batch["state"] not in TERMINAL_BATCH_STATES:
                worktree = Path(batch["worktree"])
                active.add((worktree if worktree.is_absolute() else root.parent / worktree).resolve())
        return active
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _branch_recoverable(repo: Path, branch: str) -> bool:
    upstream = _git(repo, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    if upstream.returncode != 0 or not upstream.stdout.strip().startswith("origin/"):
        return False
    upstream_ref = upstream.stdout.strip()
    tracking = _git(repo, "rev-parse", upstream_ref)
    if tracking.returncode != 0:
        return False
    try:
        remote = subprocess.run(
            [
                "git", "-C", str(repo.resolve()), "ls-remote", "--exit-code", "origin",
                f"refs/heads/{upstream_ref.removeprefix('origin/')}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if remote.returncode != 0 or remote.stdout.split("\t", 1)[0] != tracking.stdout.strip():
        return False
    return _git(repo, "merge-base", "--is-ancestor", branch, upstream_ref).returncode == 0


def _branch_allowed(root: Path, branch: str) -> bool:
    project = root / "project.json"
    try:
        pattern = json.loads(project.read_text(encoding="utf-8"))["branch_pattern"]
        return isinstance(pattern, str) and re.fullmatch(pattern, branch) is not None
    except (OSError, ValueError, KeyError, TypeError, re.error):
        return re.fullmatch(r"feature/issue-[0-9]+-.+", branch) is not None


def plan_cleanup(repo: Path, mode: str, *, min_age_hours: float = 24) -> CleanupPlan:
    """Return exact deletions and skips; never mutate the filesystem."""
    if mode not in {"soft", "hard"} or min_age_hours < 0:
        raise ValueError("cleanup mode must be soft or hard and minimum age cannot be negative")
    checkout = repo.expanduser().resolve()
    root = storage_root(checkout).resolve()
    sandboxes = sandboxes_root(checkout).resolve()
    remove: list[CleanupItem] = []
    skipped: list[dict[str, str]] = []
    registered = _registered_worktrees(checkout)

    # 1. Runs: .sandboxes/runs
    runs_dir = sandboxes / "runs"
    if runs_dir.is_dir() and not runs_dir.is_symlink():
        def _process_run_entry(path: Path, is_qa: bool = False) -> None:
            if path.is_symlink() or not _inside(root, path):
                skipped.append({"path": str(path), "reason": "not a local directory"})
            elif path.is_file():
                if _old_enough(path, min_age_hours):
                    remove.append({"kind": "file", "path": str(path)})
                else:
                    skipped.append({"path": str(path), "reason": "active or newer than minimum age"})
            elif path.is_dir():
                if is_qa and (
                    registered is None
                    or any(tree.is_relative_to(path.resolve()) for tree in registered)
                ):
                    skipped.append({"path": str(path), "reason": "registered QA worktree or Git unavailable"})
                elif _run_active(path) or not _old_enough(path, min_age_hours):
                    skipped.append({"path": str(path), "reason": "active or newer than minimum age"})
                else:
                    remove.append({"kind": "directory", "path": str(path)})

        for entry in sorted(runs_dir.iterdir()):
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and entry.name in ("tests", "qa")
                and not (entry / ".active.json").is_file()
            ):
                for child in sorted(entry.iterdir()):
                    _process_run_entry(child, is_qa=(entry.name == "qa"))
            else:
                _process_run_entry(entry, is_qa=False)

    # 2. Scratch & Logs: transit files in .sandboxes/scratch and .sandboxes/logs
    for category in ("scratch", "logs"):
        parent = sandboxes / category
        if parent.is_dir() and not parent.is_symlink():
            for path in sorted(parent.rglob("*")):
                if path.is_file() and _inside(root, path) and _old_enough(path, min_age_hours):
                    remove.append({"kind": "file", "path": str(path)})

    # 3. Legacy top-level folders outside .sandboxes: (.cache, test-logs, tmp, reports)
    for name in LEGACY_TOP_LEVEL_DIRS:
        legacy_dir = root / name
        if legacy_dir.is_dir() and not legacy_dir.is_symlink() and _inside(root, legacy_dir):
            if (
                not _old_enough(legacy_dir, min_age_hours)
                or _run_active(legacy_dir)
                or any(_run_active(sub) for sub in legacy_dir.rglob("*") if sub.is_dir())
            ):
                skipped.append({"path": str(legacy_dir), "reason": "active or newer than minimum age"})
            else:
                remove.append({"kind": "directory", "path": str(legacy_dir)})

    # Hard mode additional cleanups:
    if mode == "hard":
        # 4. Cache & Reports: .sandboxes/cache and .sandboxes/reports
        for category in ("cache", "reports"):
            parent = sandboxes / category
            if parent.is_dir() and not parent.is_symlink():
                for entry in sorted(parent.iterdir()):
                    if entry.is_file() and _inside(root, entry):
                        remove.append({"kind": "file", "path": str(entry)})
                    elif entry.is_dir() and not entry.is_symlink() and _inside(root, entry):
                        remove.append({"kind": "directory", "path": str(entry)})

        # 5. Worktrees: .sandboxes/worktrees (and legacy root/worktrees)
        worktree_roots = [sandboxes / "worktrees"]
        if (root / "worktrees").resolve() != (sandboxes / "worktrees").resolve():
            worktree_roots.append(root / "worktrees")

        active = _active_worktrees(root)
        for worktrees in worktree_roots:
            if not worktrees.is_dir() or worktrees.is_symlink():
                continue
            if registered is None or active is None:
                skipped.append({"path": str(worktrees), "reason": "Git or ledger state unavailable"})
            else:
                for path, branch in registered.items():
                    if path == checkout or not path.is_relative_to(worktrees.resolve()):
                        continue
                    reason = None
                    if not _inside(root, path) or path.parent != worktrees.resolve():
                        reason = "not a direct managed worktree"
                    elif path in active:
                        reason = "referenced by an active batch"
                    elif branch is None or not _branch_allowed(root, branch):
                        reason = "detached or unowned local branch"
                    elif not _old_enough(path, min_age_hours):
                        reason = "worktree newer than minimum age"
                    elif (status := _git(path, "status", "--porcelain", "--untracked-files=all")).returncode != 0:
                        reason = "worktree status unavailable"
                    elif status.stdout.strip():
                        reason = "worktree has uncommitted changes"
                    elif not _branch_recoverable(checkout, branch):
                        reason = "local commits are not preserved on origin"
                    if reason:
                        skipped.append({"path": str(path), "reason": reason})
                    else:
                        assert branch is not None
                        remove.append({"kind": "worktree", "path": str(path), "branch": branch})
    return {"mode": mode, "root": str(root), "min_age_hours": min_age_hours, "remove": remove, "skipped": skipped}


def _clear_read_only(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def apply_cleanup(
    repo: Path,
    plan: CleanupPlan,
    *,
    confirm: str | None = None,
) -> CleanupResult:
    """Apply a fresh plan under the same resolved `.harness` root, failing closed on drift."""
    if plan["mode"] == "hard" and confirm != "HARD":
        raise ValueError("hard cleanup requires confirm='HARD'")
    checkout = repo.expanduser().resolve()
    fresh = plan_cleanup(checkout, plan["mode"], min_age_hours=plan["min_age_hours"])
    if fresh["root"] != plan["root"] or fresh["remove"] != plan["remove"]:
        raise ValueError("cleanup plan changed; preview again before applying")
    root = Path(fresh["root"])
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    for item in fresh["remove"]:
        path = Path(item["path"])
        if not _inside(root, path):
            failed.append({"path": str(path), "reason": "path escaped storage root"})
            continue
        try:
            if item["kind"] == "worktree":
                branch = item.get("branch")
                if not branch:
                    raise OSError("worktree item missing branch name")
                result = _git(checkout, "worktree", "remove", str(path))
                if result.returncode != 0:
                    raise OSError(result.stderr.strip() or "git worktree remove failed")
                if not _branch_recoverable(checkout, branch):
                    raise OSError("local branch no longer matches its origin upstream")
                result = _git(checkout, "branch", "-D", branch)
                if result.returncode != 0:
                    raise OSError(result.stderr.strip() or "local branch removal failed")
            elif item["kind"] == "directory":
                # Windows needs an extended-length path for nested test fixtures.
                target = "\\\\?\\" + str(path) if os.name == "nt" and not str(path).startswith("\\\\?\\") else path
                shutil.rmtree(target, onexc=_clear_read_only)
            else:
                path.unlink()
            removed.append(str(path))
        except OSError as exc:
            failed.append({"path": str(path), "reason": str(exc)})
    return {"removed": removed, "failed": failed, "skipped": fresh["skipped"]}
