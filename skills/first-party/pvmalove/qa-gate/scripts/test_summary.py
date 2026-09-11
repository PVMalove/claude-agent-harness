#!/usr/bin/env python3
"""Run one quality command and expose a bounded, agent-facing test summary."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


COUNT_RE = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)\b")
DURATION_RE = re.compile(r"\bin\s+(?P<duration>[0-9.]+s)\b")
FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<nodeid>.+?)(?:\s+-\s+.*)?$")
SECRET_PATTERNS = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<REDACTED_GITHUB_TOKEN>"),
    (
        re.compile(r"(?i)(\b(?:api[_-]?key|credential|token|password|secret)\s*(?:[:=]|is)\s*)\S+"),
        r"\1<REDACTED>",
    ),
    (re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer\s+)?)\S+"), r"\1<REDACTED>"),
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command and print a bounded pytest-oriented summary.",
    )
    parser.add_argument("--log-dir", default=".harness/test-logs")
    parser.add_argument("--max-failures", type=int, default=10)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.max_failures < 1:
        parser.error("--max-failures must be positive")
    return args


def redact(line: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def render_counts(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    order = ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed")
    parts = [f"{counts[kind]} {kind}" for kind in order if kind in counts]
    return ", ".join(parts)


def summarize(command: list[str], log_dir: Path, max_failures: int) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", errors="replace", delete=False, dir=log_dir, prefix=".test-run-", suffix=".tmp"
    ) as capture:
        temporary_log = Path(capture.name)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as error:
            temporary_log.unlink(missing_ok=True)
            print("=== TEST SUMMARY ===")
            print(f"Status: ERROR (could not start command: {error})")
            return 127

        counts: dict[str, int] = {}
        pytest_duration: str | None = None
        failure_count = 0
        failures: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            sanitized = redact(line)
            capture.write(sanitized)
            found = {match.group("kind"): int(match.group("count")) for match in COUNT_RE.finditer(sanitized)}
            if found:
                counts = found
            duration_match = DURATION_RE.search(sanitized)
            if duration_match:
                pytest_duration = duration_match.group("duration")
            failure_match = FAILURE_RE.match(sanitized.strip())
            if failure_match:
                failure_count += 1
                if len(failures) < max_failures:
                    failures.append(failure_match.group("nodeid"))
        exit_code = process.wait()
        elapsed = time.monotonic() - started

    print("=== TEST SUMMARY ===")
    if exit_code == 0:
        temporary_log.unlink(missing_ok=True)
        print("Status: PASS")
        print(f"Pytest: {render_counts(counts) or 'no pytest total detected'}")
        print(f"Duration: {pytest_duration or f'{elapsed:.2f}s'}")
        return 0

    log_path = log_dir / f"test-run-{int(time.time())}-{os.getpid()}.log"
    os.replace(temporary_log, log_path)
    print(f"Status: FAIL (exit {exit_code})")
    print(f"Pytest: {render_counts(counts) or 'no pytest total detected'}")
    print(f"Duration: {pytest_duration or f'{elapsed:.2f}s'}")
    if failures:
        print("Failed tests:")
        for nodeid in failures:
            print(f"- {nodeid}")
        remaining = failure_count - len(failures)
        if remaining > 0:
            print(f"- … and {remaining} more")
    print(f"Full log: {log_path.as_posix()}")
    return exit_code


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return summarize(args.command, Path(args.log_dir), args.max_failures)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
