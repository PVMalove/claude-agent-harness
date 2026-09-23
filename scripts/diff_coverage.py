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
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_PERCENT = 70.0
MAX_UNCOVERED_LINES = 25
COVERAGE_DATA_FILE = ROOT / ".coverage"
COVERAGE_JSON_FILE = ROOT / ".coverage.diff-coverage.json"
# Verified by the clean-room run (scripts/verify.py), not by the unittest suite this gate measures, so
# its changed lines could never count as covered.
EXCLUDED_FROM_GATE = frozenset({"scripts/test_clean_room.py"})


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
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    sys.exit(
        f"diff-coverage: could not resolve a merge-base against {branch!r} or origin/{branch!r}"
    )


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _changed_lines(base: str) -> dict[str, set[int]]:
    """Return {repo-relative posix path: {added line numbers}} for *.py files changed since base.

    The diff is not narrowed by a ``*.py`` pathspec: that would hide the old name of a file renamed
    to ``.py`` and turn the whole file into added lines instead of only its real changes."""
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--find-renames",
            "--unified=0",
            "--no-color",
            base,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if diff.returncode != 0:
        sys.exit(f"diff-coverage: git diff failed: {diff.stderr.strip()}")

    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    next_line = 0
    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[len("+++ ") :]
            path = path.removeprefix("b/")
            is_gated = path.endswith(".py") and path not in EXCLUDED_FROM_GATE
            current_path = path if is_gated else None
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


def _repo_relative(path: str) -> str:
    """Normalize a coverage.json file key to a repo-relative forward-slash path: keys are OS-native
    (backslashes on Windows) and absolute when coverage ran with --source directories."""
    normalized = path.replace("\\", "/")
    root_prefix = ROOT.as_posix().rstrip("/") + "/"
    return normalized.removeprefix(root_prefix)


def source_dirs(changed: Mapping[str, set[int]]) -> list[str]:
    """Directories of the changed files, passed to ``coverage run --source`` so a changed file no test
    imports still appears in the report (with every statement missing) instead of being skipped."""
    return sorted({str(ROOT / Path(rel_path).parent) for rel_path in changed})


def summarize_coverage(
    changed: Mapping[str, set[int]], files: Mapping[str, Mapping[str, Sequence[int]]]
) -> tuple[int, int, list[str]]:
    """Count covered/total changed *statements*; blank, comment and continuation lines are never
    coverable, so they stay out of the denominator. A changed file missing from the report has no
    statement data, so every one of its changed lines counts as uncovered."""
    total = 0
    covered = 0
    uncovered: list[str] = []
    for rel_path, lines in sorted(changed.items()):
        file_report = files.get(rel_path)
        if file_report is None:
            executed: set[int] = set()
            relevant = set(lines)
        else:
            executed = set(file_report["executed_lines"])
            relevant = lines & (executed | set(file_report["missing_lines"]))
        total += len(relevant)
        for lineno in sorted(relevant):
            if lineno in executed:
                covered += 1
            else:
                uncovered.append(f"{rel_path}:{lineno}")
    return covered, total, uncovered


def meets_threshold(covered: int, total: int) -> bool:
    return covered * 100 >= total * THRESHOLD_PERCENT


def compact_uncovered(
    uncovered: Sequence[str], *, max_lines: int = MAX_UNCOVERED_LINES
) -> tuple[list[str], int]:
    """Render a bounded coverage failure summary grouped by path and line ranges.

    ``uncovered`` is already sorted by ``summarize_coverage`` and consists of the stable
    ``path:line`` form it emits. Keep the full set for the threshold calculation, but show only
    enough locations to make the failed gate actionable instead of flooding CI with its log.
    """
    selected = uncovered[:max_lines]
    by_path: dict[str, list[int]] = {}
    for entry in selected:
        path, separator, line_text = entry.rpartition(":")
        if not separator or not line_text.isdecimal():
            raise ValueError(f"invalid uncovered coverage location: {entry!r}")
        by_path.setdefault(path, []).append(int(line_text))

    rendered: list[str] = []
    for path, lines in by_path.items():
        ranges: list[str] = []
        start = previous = lines[0]
        for line_number in lines[1:]:
            if line_number == previous + 1:
                previous = line_number
                continue
            ranges.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = line_number
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        rendered.append(f"{path}:{', '.join(ranges)}")
    return rendered, len(uncovered) - len(selected)


def main() -> int:
    os.environ["PYTHONPATH"] = str(ROOT)
    base = _merge_base()
    changed = _changed_lines(base)
    if not changed:
        print(f"diff-coverage: no changed Python lines since {base}")
        return 0

    COVERAGE_DATA_FILE.unlink(missing_ok=True)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            f"--source={','.join(source_dirs(changed))}",
            "-m",
            "unittest",
            "discover",
            "-s",
            str(ROOT / "tests"),
        ],
        cwd=ROOT,
        check=False,
    )
    if run.returncode != 0:
        return run.returncode

    json_run = subprocess.run(
        # --ignore-errors: some tests exec a copy of a module from a temp directory (simulating a
        # deployed .harness/ checkout) that no longer exists by report time -- skip it rather than
        # abort the whole report, since we only care about coverage of files under this repo.
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "-o",
            str(COVERAGE_JSON_FILE),
            "-q",
            "--ignore-errors",
        ],
        cwd=ROOT,
        check=False,
    )
    if json_run.returncode != 0:
        return json_run.returncode

    report = json.loads(COVERAGE_JSON_FILE.read_text(encoding="utf-8"))
    # coverage.json keys files by OS-native path (backslashes on Windows); git diff paths are
    # always forward-slash. Normalize both sides to match regardless of platform.
    files = {
        _repo_relative(key): value for key, value in report.get("files", {}).items()
    }
    COVERAGE_DATA_FILE.unlink(missing_ok=True)
    COVERAGE_JSON_FILE.unlink(missing_ok=True)

    total_covered, total_changed, uncovered = summarize_coverage(changed, files)

    if total_changed == 0:
        print(f"diff-coverage: no coverable changed lines since {base}")
        return 0

    percent = 100.0 * total_covered / total_changed
    print(
        f"diff-coverage: {total_covered}/{total_changed} changed lines covered ({percent:.1f}%)"
    )
    if not meets_threshold(total_covered, total_changed):
        compact, omitted = compact_uncovered(uncovered)
        print(
            f"diff-coverage: below the {THRESHOLD_PERCENT:.0f}% threshold; "
            f"uncovered changed lines (showing {len(uncovered) - omitted}/{len(uncovered)}):"
        )
        for entry in compact:
            print(f"  {entry}")
        if omitted:
            print(f"  … and {omitted} more uncovered changed lines")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
