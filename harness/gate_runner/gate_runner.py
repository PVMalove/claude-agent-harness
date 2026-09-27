#!/usr/bin/env python3
"""Execute quality checks through a policy-selected checkout with safe evidence."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from ..storage import storage_path

SENSITIVE_OUTPUT: tuple[
    tuple[re.Pattern[str], str],
    tuple[re.Pattern[str], str],
    tuple[re.Pattern[str], str],
] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<REDACTED_GITHUB_TOKEN>"),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|credential|token|password|secret)\s*(?:[:=]|is)\s*)\S+"
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer\s+)?)\S+"), r"\1<redacted>"),
)


class GateRunnerError(HarnessError):
    """A policy could not prepare the requested checkout safely."""


class ExecutionPolicy(Protocol):
    """Select the checkout and isolation boundary for one gate execution."""

    def checkout(self) -> AbstractContextManager[Path]: ...


@dataclass(frozen=True)
class LocalPolicy:
    """Run the gate in the caller's existing checkout."""

    root: Path

    @contextmanager
    def checkout(self) -> Iterator[Path]:
        yield self.root.resolve()


@dataclass(frozen=True)
class CleanRoomPolicy:
    """Run the gate in a disposable worktree pinned to one candidate commit."""

    repository: Path
    candidate_commit: str

    @contextmanager
    def checkout(self) -> Iterator[Path]:
        if (
            not isinstance(self.candidate_commit, str)
            or re.fullmatch(r"[0-9a-fA-F]{7,64}", self.candidate_commit.strip()) is None
        ):
            raise GateRunnerError(
                "candidate_commit must be a hexadecimal commit SHA",
                remedy="pass candidate_commit as a 7-64 character hex commit SHA",
            )

        temporary_parent = storage_path(self.repository, "runs", "qa")
        temporary_parent.mkdir(parents=True, exist_ok=True)
        worktree_root = Path(
            tempfile.mkdtemp(prefix="agent-harness-qa-", dir=temporary_parent)
        )
        checkout = worktree_root / "checkout"
        try:
            created: subprocess.CompletedProcess[str] = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(checkout),
                    "--",
                    self.candidate_commit,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if created.returncode != 0:
                detail: str = sanitise((created.stderr or created.stdout).strip())
                raise GateRunnerError(
                    f"could not create clean QA worktree: {detail or 'unknown error'}",
                    remedy=f"inspect the git worktree error above and fix the repository/candidate commit {self.candidate_commit} before retrying",
                )
            resolved: subprocess.CompletedProcess[str] = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if (
                resolved.returncode != 0
                or resolved.stdout.strip() != self.candidate_commit
            ):
                raise GateRunnerError(
                    "clean QA worktree HEAD does not match the pinned candidate commit",
                    remedy=f"verify commit {self.candidate_commit} exists and resolves cleanly, then retry",
                )
            status: subprocess.CompletedProcess[str] = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if status.returncode != 0 or status.stdout:
                raise GateRunnerError(
                    "clean QA worktree contains mutable files",
                    remedy=(
                        "inspect the 'git status' error above and fix the worktree/repository before retrying"
                        if status.returncode != 0
                        else "the freshly created clean-room worktree should start clean -- "
                        + INTERNAL_INVARIANT_REMEDY
                    ),
                )
            yield checkout
        finally:
            if checkout.exists():
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repository),
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(checkout),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            shutil.rmtree(worktree_root, ignore_errors=True)


@dataclass(frozen=True)
class GateResult:
    """Common QA evidence for local and clean-room policies."""

    checks: list[dict[str, str]]
    artifact: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return all(check["result"] == "pass" for check in self.checks)


_LOG_COMMAND = re.compile(r"^\$ (.*)$")
_LOG_EXIT = re.compile(r"^exit_code=(-?\d+)$")


def format_command_log(command: str, returncode: int, output: str) -> str:
    """One command's block in a QA artifact log: `$ <command>`, `exit_code=<n>`, then its output."""
    return f"$ {command}\nexit_code={returncode}\n{output}\n"


def parse_command_log(lines: list[str]) -> list[tuple[str, int]]:
    """(command, exit code) of every block `format_command_log` wrote, in order."""
    commands: list[tuple[str, int]] = []
    for index, line in enumerate(lines[:-1]):
        command = _LOG_COMMAND.match(line)
        exit_code = _LOG_EXIT.match(lines[index + 1]) if command else None
        if command is not None and exit_code is not None:
            commands.append((command.group(1), int(exit_code.group(1))))
    return commands


def project_python(checkout: Path) -> Path:
    """Choose a deterministic interpreter without consulting PATH: the checkout's `.harness/.venv`
    (the dev environment `make bootstrap` creates), else the running interpreter."""
    venv_python = (
        checkout / ".harness" / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else checkout / ".harness" / ".venv" / "bin" / "python"
    )
    if venv_python.is_file():
        return venv_python
    interpreter = Path(sys.executable)
    if interpreter.is_file():
        return interpreter
    raise GateRunnerError(
        "clean-room QA has no usable explicit Python interpreter",
        remedy="create the project's .harness/.venv before QA or run the coordinator with a valid Python interpreter",
    )


_clean_room_python = project_python


def _prepared_command(
    command: str | list[str], checkout: Path
) -> tuple[str | list[str], bool]:
    """Replace a bare Python launcher before invoking a clean-room command."""
    original = command
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command, True
        if not tokens:
            return command, True
        command = tokens
    if not command:
        return command, isinstance(command, str)
    launcher = Path(command[0]).name.lower()
    if launcher not in {"python", "python3", "py"}:
        return original, isinstance(original, str)
    return [str(_clean_room_python(checkout)), *command[1:]], False


def sanitise(text: str) -> str:
    """Redact secret-shaped values before they enter an evidence artifact."""
    for pattern, replacement in SENSITIVE_OUTPUT:
        text = pattern.sub(replacement, text)
    return text


def concise_evidence(text: str) -> str:
    lines: list[str] = [
        line.strip() for line in sanitise(text).splitlines() if line.strip()
    ]
    return lines[0][:240] if lines else "no output"


def run_gate(
    commands: list[str | list[str]], policy: ExecutionPolicy, *, stop_on_failure: bool
) -> GateResult:
    """Run configured commands and return one sanitised, policy-independent result shape."""
    checks: list[dict[str, str]] = []
    outputs: list[str] = []
    started: float = time.monotonic()
    with policy.checkout() as checkout:
        for command in commands:
            prepared, shell = _prepared_command(command, checkout)
            # The artifact shows what actually ran; the check keeps the approved command verbatim,
            # which is what a completion report is matched against.
            command_text = (
                prepared
                if isinstance(prepared, str)
                else subprocess.list2cmdline(prepared)
            )
            approved_text = (
                command
                if isinstance(command, str)
                else subprocess.list2cmdline(command)
            )
            result: subprocess.CompletedProcess[str] = subprocess.run(
                prepared,
                cwd=checkout,
                shell=shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            combined: str = sanitise(
                (result.stdout or "")
                + ("\n" if result.stdout and result.stderr else "")
                + (result.stderr or "")
            )
            outputs.append(format_command_log(sanitise(command_text), result.returncode, combined))
            checks.append(
                {
                    "command": approved_text,
                    "result": "pass" if result.returncode == 0 else "fail",
                    "evidence": f"exit {result.returncode}; {concise_evidence(combined)}",
                }
            )
            if result.returncode != 0 and stop_on_failure:
                break
    return GateResult(
        checks=checks,
        artifact="\n".join(outputs),
        duration_seconds=time.monotonic() - started,
    )
