#!/usr/bin/env python3
"""Diff-coverage gate: unittest coverage of exactly the Python lines changed since the base
branch (never a repo-wide gate -- see docs/adr/0021-shared-harness-errors-base-class-with-remedy.md
and ticket #219, which introduced this alongside HarnessError so a partially-migrated legacy
module never blocks an unrelated change)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_PERCENT = 70.0
COVERAGE_DATA_FILE = ROOT / ".coverage"
COVERAGE_JSON_FILE = ROOT / ".coverage.diff-coverage.json"


def _base_branch() -> str:
    project_json = ROOT / ".harness" / "project.json"
    if project_json.is_file():
        try:
            data = json.loads(project_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        branch = data.get("base_branch")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    return "master"


def _merge_base() -> str:
    """Resolve the base commit to diff against: an explicit override, else the merge-base with
    the project's configured base_branch (falling back to a local branch of that name if there
    is no 'origin' remote, as in a fresh clone-less worktree)."""
    override = os.environ.get("DIFF_COVERAGE_BASE")
    if override:
        return override
    branch = _base_branch()
    for ref in (f"origin/{branch}", branch):
        result = subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "HEAD", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    sys.exit(f"diff-coverage: could not resolve a merge-base against {branch!r} or origin/{branch!r}")


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _changed_lines(base: str) -> dict[str, set[int]]:
    """Return {repo-relative posix path: {added line numbers}} for *.py files changed since base."""
    diff = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--unified=0", "--no-color", base, "--", "*.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if diff.returncode != 0:
        sys.exit(f"diff-coverage: git diff failed: {diff.stderr.strip()}")

    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    next_line = 0
    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[len("+++ ") :]
            current_path = None if path == "/dev/null" else path.removeprefix("b/")
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if match:
                next_line = int(match.group(1))
            continue
        if current_path is None:
            continue
        if line.startswith("+"):
            changed.setdefault(current_path, set()).add(next_line)
            next_line += 1
        elif not line.startswith("-"):
            next_line += 1
    return changed


def main() -> int:
    os.environ["PYTHONPATH"] = str(ROOT)
    base = _merge_base()
    changed = _changed_lines(base)
    if not changed:
        print(f"diff-coverage: no changed Python lines since {base}")
        return 0

    COVERAGE_DATA_FILE.unlink(missing_ok=True)
    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "unittest", "discover", "-s", str(ROOT / "tests")],
        cwd=ROOT,
    )
    if run.returncode != 0:
        return run.returncode

    json_run = subprocess.run(
        # --ignore-errors: some tests exec a copy of a module from a temp directory (simulating a
        # deployed .harness/ checkout) that no longer exists by report time -- skip it rather than
        # abort the whole report, since we only care about coverage of files under this repo.
        [sys.executable, "-m", "coverage", "json", "-o", str(COVERAGE_JSON_FILE), "-q", "--ignore-errors"],
        cwd=ROOT,
    )
    if json_run.returncode != 0:
        return json_run.returncode

    report = json.loads(COVERAGE_JSON_FILE.read_text(encoding="utf-8"))
    # coverage.json keys files by OS-native path (backslashes on Windows); git diff paths are
    # always forward-slash. Normalize both sides to match regardless of platform.
    files = {key.replace("\\", "/"): value for key, value in report.get("files", {}).items()}
    COVERAGE_DATA_FILE.unlink(missing_ok=True)
    COVERAGE_JSON_FILE.unlink(missing_ok=True)

    total_changed = 0
    total_covered = 0
    uncovered: list[str] = []
    for rel_path, lines in sorted(changed.items()):
        file_report = files.get(rel_path)
        executed = set(file_report["executed_lines"]) if file_report else set()
        excluded = set(file_report["excluded_lines"]) if file_report else set()
        relevant = lines - excluded
        total_changed += len(relevant)
        for lineno in sorted(relevant):
            if lineno in executed:
                total_covered += 1
            else:
                uncovered.append(f"{rel_path}:{lineno}")

    if total_changed == 0:
        print(f"diff-coverage: no coverable changed lines since {base}")
        return 0

    percent = 100.0 * total_covered / total_changed
    print(f"diff-coverage: {total_covered}/{total_changed} changed lines covered ({percent:.1f}%)")
    if percent < THRESHOLD_PERCENT:
        print(f"diff-coverage: below the {THRESHOLD_PERCENT:.0f}% threshold; uncovered changed lines:")
        for entry in uncovered:
            print(f"  {entry}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
