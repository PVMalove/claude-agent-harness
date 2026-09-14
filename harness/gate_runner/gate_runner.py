#!/usr/bin/env python3
"""Execute quality checks through a policy-selected checkout with safe evidence."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Iterator, Protocol


SENSITIVE_OUTPUT = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<REDACTED_GITHUB_TOKEN>"),
    (
        re.compile(r"(?i)(\b(?:api[_-]?key|credential|token|password|secret)\s*(?:[:=]|is)\s*)\S+"),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer\s+)?)\S+"), r"\1<redacted>"),
)


class GateRunnerError(Exception):
    """A policy could not prepare the requested checkout safely."""


class ExecutionPolicy(Protocol):
    """Select the checkout and isolation boundary for one gate execution."""

    def checkout(self) -> ContextManager[Path]:
        ...


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
        worktree_root = Path(tempfile.mkdtemp(prefix="agent-harness-qa-"))
        checkout = worktree_root / "checkout"
        try:
            created = subprocess.run(
                ["git", "-C", str(self.repository), "worktree", "add", "--detach", str(checkout), self.candidate_commit],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if created.returncode != 0:
                detail = sanitise((created.stderr or created.stdout).strip())
                raise GateRunnerError(f"could not create clean QA worktree: {detail or 'unknown error'}")
            resolved = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if resolved.returncode != 0 or resolved.stdout.strip() != self.candidate_commit:
                raise GateRunnerError("clean QA worktree HEAD does not match the pinned candidate commit")
            status = subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if status.returncode != 0 or status.stdout:
                raise GateRunnerError("clean QA worktree contains mutable files")
            yield checkout
        finally:
            if checkout.exists():
                subprocess.run(
                    ["git", "-C", str(self.repository), "worktree", "remove", "--force", str(checkout)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
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


def sanitise(text: str) -> str:
    """Redact secret-shaped values before they enter an evidence artifact."""
    for pattern, replacement in SENSITIVE_OUTPUT:
        text = pattern.sub(replacement, text)
    return text


def concise_evidence(text: str) -> str:
    lines = [line.strip() for line in sanitise(text).splitlines() if line.strip()]
    return lines[0][:240] if lines else "no output"


def run_gate(commands: list[str | list[str]], policy: ExecutionPolicy, *, stop_on_failure: bool) -> GateResult:
    """Run configured commands and return one sanitised, policy-independent result shape."""
    checks: list[dict[str, str]] = []
    outputs: list[str] = []
    started = time.monotonic()
    with policy.checkout() as checkout:
        for command in commands:
            command_text = command if isinstance(command, str) else subprocess.list2cmdline(command)
            result = subprocess.run(
                command,
                cwd=checkout,
                shell=isinstance(command, str),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            combined = sanitise((result.stdout or "") + ("\n" if result.stdout and result.stderr else "") + (result.stderr or ""))
            outputs.append(f"$ {sanitise(command_text)}\nexit_code={result.returncode}\n{combined}\n")
            checks.append(
                {
                    "command": command_text,
                    "result": "pass" if result.returncode == 0 else "fail",
                    "evidence": f"exit {result.returncode}; {concise_evidence(combined)}",
                }
            )
            if result.returncode != 0 and stop_on_failure:
                break
    return GateResult(checks=checks, artifact="\n".join(outputs), duration_seconds=time.monotonic() - started)
