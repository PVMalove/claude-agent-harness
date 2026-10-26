"""The failed-only rerun of ``scripts/verify.py`` (ADR 0023, section 3).

A diagnostic inside a developer session, never evidence: the selection is the exact nodeids of the
tests that failed in the previous JUnit report of the same worktree cache. A nodeid that is renamed
or gone switches the selection to ``focused``: the surviving test files of the failed tests run
whole. A selection with no test at all is ``not_run``, never a pass. Neither selection, nor a
passing result of it, ever authorizes a publish: only the full clean-room run on the final SHA does.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.errors import HarnessError
from harness.gate_runner.junit import failed_nodeids

# pytest's exit code for "no tests were collected"; an empty selection reports it as well.
NO_TESTS_EXIT = 5
FAILED_ONLY = "failed-only"
FOCUSED = "focused"
# Bound of the collection that checks the failed nodeids still exist.
_COLLECT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class FailedOnlySelection:
    """What a failed-only rerun runs: nodeids or files, how it chose them and why."""

    selection: str
    targets: tuple[str, ...]
    reason: str

    @property
    def empty(self) -> bool:
        """No test to run: the run is ``not_run``."""
        return not self.targets


def _collected(root: Path, files: list[str], env: Mapping[str, str] | None) -> set[str]:
    """The nodeids pytest collects from ``files``; empty when collection fails."""
    try:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(root),
                *files,
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_COLLECT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line for line in done.stdout.splitlines() if "::" in line}


def select(
    root: Path, junit: Path, env: Mapping[str, str] | None = None
) -> FailedOnlySelection:
    """The failed-only selection from the previous JUnit report ``junit`` of this worktree cache."""
    if not junit.is_file():
        return FailedOnlySelection(
            FAILED_ONLY, (), "there is no previous JUnit report in the worktree cache"
        )
    try:
        failures = failed_nodeids(junit.read_text(encoding="utf-8"), root)
    except (OSError, UnicodeDecodeError, HarnessError) as exc:
        return FailedOnlySelection(
            FAILED_ONLY, (), f"the previous JUnit report is unreadable ({exc})"
        )
    if not failures.nodeids and not failures.unresolved:
        return FailedOnlySelection(
            FAILED_ONLY, (), "the previous JUnit report lists no failed test"
        )
    files = list(dict.fromkeys(nodeid.split("::", 1)[0] for nodeid in failures.nodeids))
    present = _collected(root, files, env) if files else set()
    missing = [nodeid for nodeid in failures.nodeids if nodeid not in present]
    if not missing and not failures.unresolved:
        return FailedOnlySelection(
            FAILED_ONLY,
            failures.nodeids,
            f"{len(failures.nodeids)} failed nodeid(s) of the previous run",
        )
    gone = [*missing, *failures.unresolved]
    return FailedOnlySelection(
        FOCUSED,
        tuple(files),
        f"{len(gone)} failed nodeid(s) vanished or were renamed ({', '.join(gone)}); "
        f"the {len(files)} surviving test file(s) run whole",
    )
