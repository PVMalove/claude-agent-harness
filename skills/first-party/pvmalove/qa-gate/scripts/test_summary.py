#!/usr/bin/env python3
"""Run one quality command and expose a bounded, agent-facing test summary."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)\b"
)
DURATION_RE = re.compile(r"\bin\s+(?P<duration>[0-9.]+s)\b")
FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<nodeid>.+?)(?:\s+-\s+.*)?$")
TRACEBACK_START_RE = re.compile(r"^Traceback \(most recent call last\):$")
EXCEPTION_RE = re.compile(
    r"^(?:E\s+)?(?P<exception>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Fault|Failure))"
    r"(?::\s*(?P<message>.*))?$"
)
TOOL_ERROR_RE = re.compile(
    r"^(?P<location>.+?:\d+(?::\d+)?)\s*:\s*"
    r"(?P<level>fatal\s+error|error)\s*:\s*(?P<message>.+)$",
    re.IGNORECASE,
)
ERROR_MESSAGE_RE = re.compile(r"^(?:ERROR|FATAL):\s*(?P<message>.+)$")
_GATE_RUNNER: ModuleType | None = None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command and print a bounded pytest-oriented summary.",
    )
    parser.add_argument("--log-dir", default=".harness/test-logs")
    parser.add_argument("--max-failures", type=int, default=10)
    parser.add_argument("--max-diagnostics", type=int, default=10)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.max_failures < 1:
        parser.error("--max-failures must be positive")
    if args.max_diagnostics < 1:
        parser.error("--max-diagnostics must be positive")
    return args


def redact(line: str) -> str:
    return str(_gate_runner().sanitise(line))


def _gate_runner() -> ModuleType:
    """Load the managed shared runner without assuming a Python package install.

    gate_runner.py is an ordinary submodule of the ``harness``/``.harness`` package (it does
    ``from ..errors import HarnessError``), so it must be imported through that package rather
    than exec'd standalone -- alias ``harness`` to whichever of ``harness``/``.harness`` this
    tool actually finds, mirroring the bootstrap in coordinator.py/orca_adapter.py/delivery_stats.py.
    """
    global _GATE_RUNNER
    if _GATE_RUNNER is not None:
        return _GATE_RUNNER
    for parent in Path(__file__).resolve().parents:
        for relative in (
            Path(".harness/gate_runner/gate_runner.py"),
            Path("harness/gate_runner/gate_runner.py"),
        ):
            path = parent / relative
            if not path.is_file():
                continue
            harness_root = path.parent.parent
            repo_root = harness_root.parent
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            if harness_root.name != "harness":
                spec = importlib.util.spec_from_file_location(
                    "harness",
                    harness_root / "__init__.py",
                    submodule_search_locations=[str(harness_root)],
                )
                if spec is None or spec.loader is None:
                    break
                package = importlib.util.module_from_spec(spec)
                sys.modules["harness"] = package
                spec.loader.exec_module(package)
            _GATE_RUNNER = importlib.import_module("harness.gate_runner.gate_runner")
            return _GATE_RUNNER
    raise RuntimeError("shared gate-runner is missing; run harness update")


def render_counts(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    order = ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed")
    parts = [f"{counts[kind]} {kind}" for kind in order if kind in counts]
    return ", ".join(parts)


def collect_diagnostics(lines: list[str], max_diagnostics: int) -> list[str]:
    """Extract stable failure signals instead of returning an arbitrary log tail.

    Commands wrapped by this tool are not necessarily pytest, so this deliberately recognizes
    common Python tracebacks and compiler/linter diagnostics without requiring an output plugin.
    The input has already been sanitized before it reaches this function.
    """
    diagnostics: list[str] = []
    in_traceback = False

    def add(kind: str, message: str) -> None:
        entry = f"{kind}: {message.strip()}"
        if message.strip() and entry not in diagnostics and len(diagnostics) < max_diagnostics:
            diagnostics.append(entry)

    for line in lines:
        stripped = line.strip()
        # gate_runner records the invoked command as "$ ...". Its quoted arguments may look
        # like diagnostics, but they are input, not a failure emitted by the command.
        if stripped.startswith("$ "):
            continue
        if TRACEBACK_START_RE.match(stripped):
            in_traceback = True
            continue

        exception = EXCEPTION_RE.match(stripped)
        if exception:
            message = exception.group("exception")
            if exception.group("message"):
                message = f"{message}: {exception.group('message')}"
            add("Traceback" if in_traceback else "Exception", message)
            in_traceback = False
            continue

        tool_error = TOOL_ERROR_RE.match(stripped)
        if tool_error:
            add("Error", f"{tool_error.group('location')}: {tool_error.group('message')}")
            continue

        error_message = ERROR_MESSAGE_RE.match(stripped)
        if error_message:
            add("Error", error_message.group("message"))

    return diagnostics


def summarize(
    command: list[str], log_dir: Path, max_failures: int, max_diagnostics: int
) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        errors="replace",
        delete=False,
        dir=log_dir,
        prefix=".test-run-",
        suffix=".tmp",
    ) as capture:
        temporary_log = Path(capture.name)
        try:
            result = _gate_runner().run_gate(
                [command], _gate_runner().LocalPolicy(Path.cwd()), stop_on_failure=True
            )
        except (OSError, RuntimeError) as error:
            temporary_log.unlink(missing_ok=True)
            print("=== TEST SUMMARY ===")
            print(f"Status: ERROR (could not start command: {error})")
            return 127

        counts: dict[str, int] = {}
        pytest_duration: str | None = None
        failure_count = 0
        failures: list[str] = []
        sanitized_lines: list[str] = []
        for line in result.artifact.splitlines(keepends=True):
            sanitized = redact(line)
            capture.write(sanitized)
            sanitized_lines.append(sanitized)
            found = {
                match.group("kind"): int(match.group("count"))
                for match in COUNT_RE.finditer(sanitized)
            }
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
        exit_code = (
            0
            if result.passed
            else next(
                int(check["evidence"].split(";", 1)[0].removeprefix("exit "))
                for check in result.checks
                if check["result"] == "fail"
            )
        )
        elapsed = result.duration_seconds

    diagnostics = collect_diagnostics(sanitized_lines, max_diagnostics)

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
    if diagnostics:
        print("Diagnostics:")
        for diagnostic in diagnostics:
            print(f"- {diagnostic}")
    print(f"Full log: {log_path.as_posix()}")
    return exit_code


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return summarize(
        args.command, Path(args.log_dir), args.max_failures, args.max_diagnostics
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
